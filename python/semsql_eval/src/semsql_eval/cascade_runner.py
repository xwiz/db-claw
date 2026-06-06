"""Wire the Spider/BIRD harness to the Rust cascade.

The cascade lives in `crates/semsql-cli` (Rust binary `semsql`); this
module shells out to it via subprocess so the harness stays
language-agnostic and benchmark runs use the same surface as the CLI.

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
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .spider import Example

__all__ = [
    "CascadeQueryResult",
    "CascadeRunnerError",
    "build_graph_for_db",
    "build_graph_for_db_url",
    "make_cascade_predictor",
    "run_cascade_query",
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
    vocab_jsonl: Path | None = None,
    schema_description_dir: Path | None = None,
) -> Path:
    """Run ``semsql extract --framework none --db-url sqlite:<path>``.

    Idempotent: if ``output`` already exists, returns it unchanged.
    Atomic-ish: writes to ``output.tmp`` then renames, so a SIGINT
    mid-extract doesn't leave a partial file masquerading as complete.
    """
    if _graph_cache_ready(output):
        return output
    if output.exists():
        output.unlink()
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
    if vocab_jsonl is not None:
        cmd.extend(["--vocab-jsonl", str(vocab_jsonl)])
    if schema_description_dir is not None:
        cmd.extend(["--schema-description-dir", str(schema_description_dir)])
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
        check=False,
        env=_cascade_subprocess_env(semsql_bin),
    )
    if proc.returncode != 0 or not _graph_cache_ready(tmp):
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


def build_graph_for_db_url(
    semsql_bin: Path,
    db_url: str,
    output: Path,
    *,
    path_arg: Path | None = None,
    timeout_seconds: int = 60,
    sample_values: bool = True,
    vocab_jsonl: Path | None = None,
    schema_description_dir: Path | None = None,
) -> Path:
    """Run ``semsql extract --framework none`` against any supported DB URL."""
    if _graph_cache_ready(output):
        return output
    if output.exists():
        output.unlink()
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_suffix(output.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()

    cmd = [
        str(semsql_bin),
        "extract",
        str(path_arg or output.parent),
        "--framework",
        "none",
        "--db-url",
        db_url,
        "--output",
        str(tmp),
    ]
    if not sample_values:
        cmd.append("--no-sample-values")
    if vocab_jsonl is not None:
        cmd.extend(["--vocab-jsonl", str(vocab_jsonl)])
    if schema_description_dir is not None:
        cmd.extend(["--schema-description-dir", str(schema_description_dir)])
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
        check=False,
        env=_cascade_subprocess_env(semsql_bin),
    )
    if proc.returncode != 0 or not _graph_cache_ready(tmp):
        if tmp.exists():
            tmp.unlink()
        raise CascadeRunnerError(
            f"semsql extract failed (exit={proc.returncode}) for {db_url}:\n"
            f"  stdout: {proc.stdout.strip()}\n"
            f"  stderr: {proc.stderr.strip()}"
        )
    tmp.replace(output)
    return output


def _graph_cache_ready(path: Path) -> bool:
    try:
        return path.exists() and path.stat().st_size > 0
    except OSError:
        return False


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
    """Number of Stage 2 re-decode attempts triggered by validator-rejected
    identifiers. ``0`` on a clean run, ``> 0`` when constrained generation hit
    a repairable failure. Surfaced into ``--report-json`` per-example records
    so eval runs can chart constrained-vs-unconstrained drift over time."""

    stage3_slots: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    """Structured Stage 3 slot decisions emitted by `semsql query` when
    available. Empty for Stage 0a, non-ONNX runs, and old binaries."""

    query_frame: dict[str, Any] | None = None
    """Full query-frame payload emitted by ``--query-frame-json`` when the
    caller requests it. Includes the pre-Stage-3 candidate contract and the
    Stage 3 scored decisions when available."""

    error_detail: str | None = None
    """Compact stderr/error detail for non-success cascade runs."""

    elapsed_seconds: float = 0.0
    """Wall-clock seconds spent in the ``semsql query`` subprocess."""

    stdout_bytes: int = 0
    """UTF-8 byte length captured from subprocess stdout."""

    stderr_bytes: int = 0
    """UTF-8 byte length captured from subprocess stderr."""

    timed_out_after_seconds: int | None = None
    """Configured subprocess timeout when ``stage_pinned == "timeout"``."""

    stage_timings_us: dict[str, int] = dataclasses.field(default_factory=dict)
    """Internal cascade stage timings emitted by the CLI, in microseconds."""


def run_cascade_query(
    semsql_bin: Path,
    graph: Path,
    nl: str,
    *,
    timeout_seconds: int = 30,
    cascade_manifest: Path | None = None,
    intent_yaml: Path | None = None,
    dialect: str | None = None,
    oracle_skeleton: str | None = None,
    oracle_schema_json: str | None = None,
    oracle_slots_json: str | None = None,
    query_frame_json: Path | None = None,
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
    cmd = [str(semsql_bin), "query", "--graph", str(graph)]
    if cascade_manifest is not None:
        cmd.extend(["--cascade-manifest", str(cascade_manifest)])
    if intent_yaml is not None:
        cmd.extend(["--intent-yaml", str(intent_yaml)])
    if dialect is not None:
        cmd.extend(["--dialect", dialect])
    if oracle_skeleton is not None:
        cmd.extend(["--oracle-skeleton", oracle_skeleton])
    if oracle_schema_json is not None:
        cmd.extend(["--oracle-schema-json", oracle_schema_json])
    if oracle_slots_json is not None:
        cmd.extend(["--oracle-slots-json", oracle_slots_json])
    if query_frame_json is not None:
        query_frame_json.parent.mkdir(parents=True, exist_ok=True)
        if query_frame_json.exists():
            query_frame_json.unlink()
        cmd.extend(["--query-frame-json", str(query_frame_json)])
    cmd.append(nl)
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
            env=_cascade_subprocess_env(semsql_bin),
        )
    except subprocess.TimeoutExpired:
        elapsed = time.perf_counter() - started
        return CascadeQueryResult(
            sql=None,
            stage_pinned="timeout",
            error_detail=f"semsql query timed out after {timeout_seconds}s",
            elapsed_seconds=elapsed,
            timed_out_after_seconds=timeout_seconds,
        )
    elapsed = time.perf_counter() - started
    stdout_bytes = len(proc.stdout.encode("utf-8", errors="replace"))
    stderr_bytes = len(proc.stderr.encode("utf-8", errors="replace"))
    stage = _parse_stage_pinned(proc.stderr) or ("stage_0a" if proc.returncode == 0 else "error")
    stage = _classify_failure_stage(
        proc.stderr,
        fallback_stage=stage,
        cascade_manifest=cascade_manifest,
        returncode=proc.returncode,
    )
    repair = _parse_repair_attempts(proc.stderr)
    stage3_slots = _parse_stage3_slots(proc.stderr)
    stage_timings_us = _parse_stage_timings_us(proc.stderr)
    query_frame = _load_query_frame(query_frame_json)
    if proc.returncode != 0:
        return CascadeQueryResult(
            sql=None,
            stage_pinned=stage,
            repair_attempts=repair,
            stage3_slots=stage3_slots,
            query_frame=query_frame,
            error_detail=_compact_stderr(proc.stderr),
            elapsed_seconds=elapsed,
            stdout_bytes=stdout_bytes,
            stderr_bytes=stderr_bytes,
            stage_timings_us=stage_timings_us,
        )
    sql = proc.stdout.strip()
    return CascadeQueryResult(
        sql=sql or None,
        stage_pinned=stage,
        repair_attempts=repair,
        stage3_slots=stage3_slots,
        query_frame=query_frame,
        elapsed_seconds=elapsed,
        stdout_bytes=stdout_bytes,
        stderr_bytes=stderr_bytes,
        stage_timings_us=stage_timings_us,
    )


def _compact_stderr(stderr: str, *, max_len: int = 2000) -> str | None:
    text = (stderr or "").strip()
    if not text:
        return None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    compact = "\n".join(lines[-12:])
    if len(compact) > max_len:
        return compact[: max_len - 3] + "..."
    return compact


_STAGE_RX = re.compile(r"^stage_pinned=([a-z0-9_]+)\s*$", re.MULTILINE)
_REPAIR_RX = re.compile(r"^repair_attempts=(\d+)\s*$", re.MULTILINE)
_STAGE3_SLOTS_RX = re.compile(r"^stage3_slots=(.+)\s*$", re.MULTILINE)
_STAGE_TIMINGS_RX = re.compile(
    r"^stage_0a=(\d+)us stage_0b=(\d+)us stage_1=(\d+)us "
    r"stage_2=(\d+)us stage_3=(\d+)us stage_4=(\d+)us\s*$",
    re.MULTILINE,
)
_MISSING_ONNX_HINT = "model stages require"


def _parse_stage_pinned(stderr: str) -> str | None:
    """Pull the most-recent ``stage_pinned=...`` tag out of stderr.

    The binary emits at most one tag per run today, but we take the
    last match defensively so a future change to emit progress tags
    (e.g. one per stage) doesn't break the parser."""
    matches = _STAGE_RX.findall(stderr or "")
    return matches[-1] if matches else None


def _classify_failure_stage(
    stderr: str,
    *,
    fallback_stage: str,
    cascade_manifest: Path | None,
    returncode: int,
) -> str:
    """Promote missing-ONNX model bails to an infrastructure bucket.

    Without this, a stale non-ONNX ``target/debug/semsql.exe`` looks like
    an ordinary ``needs_model`` bail and can silently poison trace-corpus
    generation with zero Stage 3 records.
    """
    if returncode != 0 and cascade_manifest is not None and _MISSING_ONNX_HINT in stderr:
        return "missing_onnx_feature"
    return fallback_stage


def _parse_repair_attempts(stderr: str) -> int:
    """Parse the ``repair_attempts=N`` tag — `0` if absent."""
    matches = _REPAIR_RX.findall(stderr or "")
    if not matches:
        return 0
    try:
        return int(matches[-1])
    except ValueError:
        return 0


def _parse_stage3_slots(stderr: str) -> list[dict[str, Any]]:
    """Parse structured Stage 3 slot diagnostics from stderr."""
    matches = _STAGE3_SLOTS_RX.findall(stderr or "")
    if not matches:
        return []
    try:
        parsed = json.loads(matches[-1])
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [row for row in parsed if isinstance(row, dict)]


def _parse_stage_timings_us(stderr: str) -> dict[str, int]:
    matches = _STAGE_TIMINGS_RX.findall(stderr or "")
    if not matches:
        return {}
    labels = ("stage_0a", "stage_0b", "stage_1", "stage_2", "stage_3", "stage_4")
    try:
        return dict(zip(labels, (int(value) for value in matches[-1]), strict=True))
    except ValueError:
        return {}


def _load_query_frame(path: Path | None) -> dict[str, Any] | None:
    """Load the JSON payload written by ``semsql query --query-frame-json``."""
    if path is None or not path.exists():
        return None
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


_ORT_LIBRARY_NAMES = ("onnxruntime.dll", "libonnxruntime.so", "libonnxruntime.dylib")


def _cascade_subprocess_env(semsql_bin: Path) -> dict[str, str]:
    """Return subprocess env with the repo-local ORT dylib pinned if present."""
    env = os.environ.copy()
    if env.get("ORT_DYLIB_PATH"):
        return env
    for candidate in _ort_library_candidates(semsql_bin):
        if candidate.exists():
            env["ORT_DYLIB_PATH"] = str(candidate.resolve())
            break
    return env


def _ort_library_candidates(semsql_bin: Path) -> list[Path]:
    """Likely ONNX Runtime library locations for local dev and CI builds."""
    try:
        repo_root = Path(__file__).resolve().parents[4]
    except IndexError:  # pragma: no cover - only possible in unusual installs.
        repo_root = Path.cwd()
    dirs = [
        semsql_bin.resolve().parent,
        semsql_bin.resolve().parent.parent / "release",
        semsql_bin.resolve().parent.parent / "debug",
        repo_root / "target" / "release",
        repo_root / "target" / "debug",
    ]
    out: list[Path] = []
    seen: set[Path] = set()
    for directory in dirs:
        for name in _ORT_LIBRARY_NAMES:
            candidate = directory / name
            if candidate not in seen:
                out.append(candidate)
                seen.add(candidate)
    return out


# ---------------------------------------------------------------------------
# predictor factory
# ---------------------------------------------------------------------------


def make_cascade_predictor(
    semsql_bin: Path,
    graph_cache_dir: Path,
    *,
    sentinel_sql: str = "SELECT 1",
    extract_timeout_seconds: int = 60,
    query_timeout_seconds: int = 60,
    on_stage: Callable[[Example, str, int], None] | None = None,
    on_query_result: Callable[[Example, CascadeQueryResult], None] | None = None,
    cascade_manifest: Path | None = None,
    intent_yaml: Path | None = None,
    dialect: str | None = None,
    oracle_skeleton: Callable[[Example], str | None] | None = None,
    oracle_schema_json: Callable[[Example], str | None] | None = None,
    oracle_slots_json: Callable[[Example], str | None] | None = None,
    query_frame_dir: Path | None = None,
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

    ``oracle_skeleton``, ``oracle_schema_json``, and
    ``oracle_slots_json`` are diagnostic callbacks used by ablation runs.
    They are evaluated per example and forwarded to hidden `semsql query`
    flags; normal benchmark runs leave them unset.

    ``query_frame_dir`` enables per-example JSON frame capture. The loaded
    frame is attached to ``CascadeQueryResult.query_frame`` for report writers.
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
    query_frame_root = query_frame_dir.resolve() if query_frame_dir is not None else None
    if query_frame_root is not None:
        query_frame_root.mkdir(parents=True, exist_ok=True)

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
            raise CascadeRunnerError(f"db_id {db_id!r} resolves outside cache_root") from e
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
            dialect=dialect,
            oracle_skeleton=oracle_skeleton(example) if oracle_skeleton is not None else None,
            oracle_schema_json=(
                oracle_schema_json(example) if oracle_schema_json is not None else None
            ),
            oracle_slots_json=(
                oracle_slots_json(example) if oracle_slots_json is not None else None
            ),
            query_frame_json=(
                _query_frame_path(query_frame_root, example)
                if query_frame_root is not None
                else None
            ),
        )
        if on_stage is not None:
            on_stage(example, result.stage_pinned, result.repair_attempts)
        if on_query_result is not None:
            on_query_result(example, result)
        return result.sql or sentinel_sql

    return predict


def _query_frame_path(root: Path, example: Example) -> Path:
    """Stable per-example frame path under ``root``."""
    digest = hashlib.sha1(
        f"{example.db_id}\0{example.question}\0{example.gold_sql}".encode()
    ).hexdigest()[:16]
    return root / f"{example.db_id}-{digest}.json"
