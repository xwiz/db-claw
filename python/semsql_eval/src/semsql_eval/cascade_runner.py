"""Wire the Spider/BIRD harness to the Rust cascade.

The cascade lives in `crates/semsql-cli` (Rust binary `semsql`); this
module shells out to it via subprocess so the harness stays
language-agnostic. PyO3 bindings are deferred to v0.5 — subprocess
gives us identical surface for less integration cost.

Two entry points:

  - :func:`build_graph_for_db` extracts a `.semsql` SemanticGraph from
    one Spider/BIRD SQLite database. Cached on disk so the harness
    builds each DB at most once per evaluation run.
  - :func:`make_cascade_predictor` returns a `predict(Example) -> str`
    callable that the existing :func:`semsql_eval.spider.evaluate`
    runner consumes directly. On cascade failure (NeedsModel before
    weights ship, parse errors, etc.) the predictor returns a sentinel
    "SELECT 1" SQL — exec-acc scoring will count these as wrong, which
    is exactly what we want for the unimplemented model stages.

Usage from the eval CLI::

    from semsql_eval.spider import SpiderSuite, evaluate
    from semsql_eval.cascade_runner import make_cascade_predictor

    suite = SpiderSuite.load(Path("data/spider/dev.json"),
                             Path("data/spider/database"))
    predict = make_cascade_predictor(
        semsql_bin=Path("target/debug/semsql.exe"),
        graph_cache_dir=Path("target/spider_graphs"),
    )
    summary = evaluate(suite, predict)
    print(f"exec-acc: {summary.exec_acc:.3%}  errored: {summary.error_rate:.3%}")
"""

from __future__ import annotations

import dataclasses
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from .spider import Example

__all__ = [
    "CascadeQueryResult",
    "CascadeRunnerError",
    "build_graph_for_db",
    "run_cascade_query",
    "make_cascade_predictor",
]


class CascadeRunnerError(RuntimeError):
    """Raised on a non-recoverable error from the `semsql` binary —
    e.g. binary missing, schema-extract crash. Per-query NL→SQL
    failures are *not* errors at this layer; they return a sentinel
    SQL so the eval harness can keep going."""


# ---------------------------------------------------------------------------
# graph extraction
# ---------------------------------------------------------------------------


def build_graph_for_db(
    semsql_bin: Path,
    sqlite_db: Path,
    output: Path,
    *,
    timeout_seconds: int = 60,
) -> Path:
    """Run ``semsql extract --framework none --db-url sqlite:<path>``.

    Idempotent: if ``output`` already exists, returns it unchanged.
    Atomic-ish: writes to ``output.tmp`` then renames, so a SIGINT
    mid-extract doesn't leave a partial file masquerading as complete.
    """
    if output.exists():
        return output
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(output.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()

    cmd = [
        str(semsql_bin),
        "extract",
        str(sqlite_db.parent),  # path arg — unused by --framework=none
        "--framework",
        "none",
        "--db-url",
        f"sqlite:{sqlite_db.resolve()}",
        "--output",
        str(tmp),
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout_seconds, check=False
    )
    if proc.returncode != 0 or not tmp.exists():
        # Don't leave a half-written tmp around.
        if tmp.exists():
            tmp.unlink()
        raise CascadeRunnerError(
            f"semsql extract failed (exit={proc.returncode}) for {sqlite_db}:\n"
            f"  stdout: {proc.stdout.strip()}\n"
            f"  stderr: {proc.stderr.strip()}"
        )
    tmp.replace(output)
    return output


# ---------------------------------------------------------------------------
# query execution
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class CascadeQueryResult:
    """Output of one cascade run.

    ``sql`` is None on bail / timeout / parse failure. ``stage_pinned``
    reflects which stage produced the answer (or where it bailed) per
    the `stage_pinned=...` tag the binary emits on stderr.
    """

    sql: str | None
    stage_pinned: str
    """One of ``"stage_0a"``, ``"stage_1"`` etc. for success paths;
    ``"needs_model"`` when Stage 1+ would be required but isn't wired;
    ``"timeout"`` when the binary timed out; ``"error"`` for any other
    fault. The set is open — future stages add new tags as the cascade
    grows."""

    repair_attempts: int = 0
    """Phase E observability counter — number of Stage 2 re-decode
    attempts triggered by validator-rejected identifiers. ``0`` on a
    clean run, ``> 0`` when llguidance-constrained generation hit a
    repairable failure. Surfaced into ``--report-json`` per-example
    records so eval runs can chart constrained-vs-unconstrained drift
    over time."""


def run_cascade_query(
    semsql_bin: Path,
    graph: Path,
    nl: str,
    *,
    timeout_seconds: int = 30,
    cascade_manifest: Path | None = None,
    intent_yaml: Path | None = None,
) -> CascadeQueryResult:
    """Return the cascade's outcome for ``nl``.

    Emits a structured result so callers can bucket per-stage. The
    ``sql`` field is ``None`` on every non-success path; the
    ``stage_pinned`` tag distinguishes the bail reason.

    ``cascade_manifest`` opts into Stage 1+ model inference (requires the
    binary to be built with ``--features onnx``). ``intent_yaml``
    threads an intent pattern library through Stage 0b. Both are passed
    verbatim to ``semsql query``.
    """
    cmd = [str(semsql_bin), "query", "--graph", str(graph), nl]
    if cascade_manifest is not None:
        cmd.extend(["--cascade-manifest", str(cascade_manifest)])
    if intent_yaml is not None:
        cmd.extend(["--intent-yaml", str(intent_yaml)])
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return CascadeQueryResult(sql=None, stage_pinned="timeout")
    stage = _parse_stage_pinned(proc.stderr) or (
        "stage_0a" if proc.returncode == 0 else "error"
    )
    repair = _parse_repair_attempts(proc.stderr)
    if proc.returncode != 0:
        return CascadeQueryResult(
            sql=None, stage_pinned=stage, repair_attempts=repair
        )
    sql = proc.stdout.strip()
    return CascadeQueryResult(
        sql=sql or None, stage_pinned=stage, repair_attempts=repair
    )


_STAGE_RX = re.compile(r"^stage_pinned=([a-z0-9_]+)\s*$", re.MULTILINE)
_REPAIR_RX = re.compile(r"^repair_attempts=(\d+)\s*$", re.MULTILINE)


def _parse_stage_pinned(stderr: str) -> str | None:
    """Pull the most-recent ``stage_pinned=...`` tag out of stderr.

    The binary emits at most one tag per run today, but we take the
    last match defensively so a future change to emit progress tags
    (e.g. one per stage) doesn't break the parser."""
    matches = _STAGE_RX.findall(stderr or "")
    return matches[-1] if matches else None


def _parse_repair_attempts(stderr: str) -> int:
    """Parse the ``repair_attempts=N`` tag — `0` if absent."""
    matches = _REPAIR_RX.findall(stderr or "")
    if not matches:
        return 0
    try:
        return int(matches[-1])
    except ValueError:
        return 0


# ---------------------------------------------------------------------------
# predictor factory
# ---------------------------------------------------------------------------


def make_cascade_predictor(
    semsql_bin: Path,
    graph_cache_dir: Path,
    *,
    sentinel_sql: str = "SELECT 1",
    extract_timeout_seconds: int = 60,
    query_timeout_seconds: int = 30,
    on_stage: Callable[[Example, str], None] | None = None,
    cascade_manifest: Path | None = None,
    intent_yaml: Path | None = None,
) -> Callable[[Example], str]:
    """Return a `predict(Example) -> str` for use with
    :func:`semsql_eval.spider.evaluate`.

    The predictor:

      1. Resolves a per-DB graph file inside ``graph_cache_dir``,
         building it on first use.
      2. Runs the cascade against the example's question.
      3. Returns the SQL string, or ``sentinel_sql`` on bail.

    The sentinel approach (instead of raising) is deliberate: it lets
    ``evaluate`` count cascade-bails as wrong answers (correct for
    benchmark accuracy) rather than errors. The error column in the
    eval summary is reserved for predictor crashes — a crash is a bug
    in the eval wiring, not a benchmark miss.

    ``cascade_manifest`` and ``intent_yaml`` are forwarded to every
    cascade run. With a manifest in scope, queries that fall through
    Stage 0a get routed through Stage 1 (schema linker) + grammar
    compile — surfacing per-stage tags like ``needs_model_stage_2``
    once the model stages emit those (today the orchestrator still
    pins at ``needs_model``; the manifest just makes Stage 1 actually
    execute).
    """
    if not semsql_bin.exists():
        # Look in PATH as a fallback so callers don't have to point
        # at an absolute path on a typical dev box.
        which = shutil.which("semsql")
        if which is not None:
            semsql_bin = Path(which)
        else:
            raise CascadeRunnerError(
                f"semsql binary not found at {semsql_bin} and not on PATH; "
                "build it with `cargo build -p semsql-cli`"
            )

    graph_cache_dir.mkdir(parents=True, exist_ok=True)
    cache_root = graph_cache_dir.resolve()

    def _graph_path(example: Example) -> Path:
        # Defensive path-traversal guard. Spider/BIRD db_ids are
        # snake_case and safe in practice, but the eval harness can be
        # invoked against arbitrary corpora — a hostile manifest could
        # contain `db_id = "../../etc/passwd"`. Reject anything that
        # escapes the cache root or contains path separators.
        db_id = example.db_id
        # Reject:
        #  - empty / dot-prefixed names (`.`, `..`, hidden files)
        #  - path separators (POSIX + Windows)
        #  - drive-letter abuse on Windows (`C:foo`) which makes
        #    `cache_root / db_id` resolve to an absolute path elsewhere
        #  - NUL byte (some libc paths truncate at \0, masking traversal)
        if (
            not db_id
            or "/" in db_id
            or "\\" in db_id
            or ":" in db_id
            or "\x00" in db_id
            or db_id in (".", "..")
            or db_id.startswith(".")
        ):
            raise CascadeRunnerError(
                f"refusing unsafe db_id {db_id!r} — contains path separator, "
                "drive-letter prefix, NUL byte, or escapes the cache root"
            )
        candidate = (cache_root / f"{db_id}.semsql").resolve()
        try:
            candidate.relative_to(cache_root)
        except ValueError as e:
            raise CascadeRunnerError(
                f"db_id {db_id!r} resolves outside cache_root"
            ) from e
        return candidate

    def predict(example: Example) -> str:
        graph_path = _graph_path(example)
        try:
            build_graph_for_db(
                semsql_bin,
                example.db_path,
                graph_path,
                timeout_seconds=extract_timeout_seconds,
            )
        except CascadeRunnerError:
            # If we can't even build a graph for the DB, every query
            # against it bails. Sentinel = wrong answer.
            return sentinel_sql

        result = run_cascade_query(
            semsql_bin,
            graph_path,
            example.question,
            timeout_seconds=query_timeout_seconds,
            cascade_manifest=cascade_manifest,
            intent_yaml=intent_yaml,
        )
        if on_stage is not None:
            on_stage(example, result.stage_pinned, result.repair_attempts)
        return result.sql or sentinel_sql

    return predict
