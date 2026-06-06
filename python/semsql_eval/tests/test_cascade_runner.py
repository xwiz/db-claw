"""Tests for `cascade_runner` — exercise the subprocess wrapper end-to-end
against the real `semsql` binary when it's available, and the bail
paths when it isn't."""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
from semsql_eval.cascade_runner import (
    CascadeRunnerError,
    build_graph_for_db,
    build_graph_for_db_url,
    make_cascade_predictor,
    run_cascade_query,
)
from semsql_eval.spider import Example

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _semsql_binary() -> Path | None:
    """Locate the freshly-built semsql binary in cargo target/, falling
    back to PATH. Returns None if neither is available — tests that
    require it skip gracefully."""
    explicit = os.environ.get("SEMSQL_BIN")
    if explicit:
        return Path(explicit).resolve()
    candidates = [
        Path("target/debug/semsql.exe"),
        Path("target/debug/semsql"),
        Path("target/release/semsql.exe"),
        Path("target/release/semsql"),
    ]
    for c in candidates:
        if c.exists():
            return c.resolve()
    cargo = shutil.which("cargo")
    if cargo is not None:
        subprocess.run(
            [cargo, "build", "-p", "semsql-cli"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        for c in candidates:
            if c.exists():
                return c.resolve()
    found = shutil.which("semsql")
    return Path(found) if found else None


def _build_demo_sqlite(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE tenants (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            tenant_id INTEGER NOT NULL,
            email TEXT NOT NULL
        );
        INSERT INTO tenants VALUES (1, 'Acme'), (2, 'Globex');
        INSERT INTO users VALUES (1, 1, 'a@x'), (2, 2, 'b@x');
        """
    )
    conn.close()


@pytest.fixture(scope="session")
def semsql_bin() -> Path:
    bin_path = _semsql_binary()
    if bin_path is None:
        pytest.skip("semsql binary not available — build with `cargo build -p semsql-cli`")
    return bin_path


def test_build_graph_is_idempotent(tmp_path: Path, semsql_bin: Path) -> None:
    src = tmp_path / "demo.sqlite"
    _build_demo_sqlite(src)
    out = tmp_path / "demo.semsql"

    build_graph_for_db(semsql_bin, src, out)
    assert out.exists()
    first_mtime = out.stat().st_mtime_ns

    # Second call: existing graph short-circuits.
    build_graph_for_db(semsql_bin, src, out)
    assert out.stat().st_mtime_ns == first_mtime


def test_build_graph_rebuilds_zero_byte_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import semsql_eval.cascade_runner as cascade_runner

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        output = Path(cmd[cmd.index("--output") + 1])
        output.write_text("graph", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(cascade_runner.subprocess, "run", fake_run)

    src = tmp_path / "demo.sqlite"
    src.write_bytes(b"stubbed")
    out = tmp_path / "demo.semsql"
    out.write_bytes(b"")

    build_graph_for_db(Path("target/debug/semsql.exe"), src, out)

    assert len(calls) == 1
    assert out.read_text(encoding="utf-8") == "graph"


def test_build_graph_rejects_zero_byte_extract_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import semsql_eval.cascade_runner as cascade_runner

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        output = Path(cmd[cmd.index("--output") + 1])
        output.write_bytes(b"")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(cascade_runner.subprocess, "run", fake_run)

    src = tmp_path / "demo.sqlite"
    src.write_bytes(b"stubbed")
    out = tmp_path / "demo.semsql"

    with pytest.raises(CascadeRunnerError):
        build_graph_for_db(Path("target/debug/semsql.exe"), src, out)

    assert not out.exists()
    assert not (out.with_suffix(out.suffix + ".tmp")).exists()


def test_build_graph_forwards_vocab_and_schema_description_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import semsql_eval.cascade_runner as cascade_runner

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        output = Path(cmd[cmd.index("--output") + 1])
        output.write_text("graph", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(cascade_runner.subprocess, "run", fake_run)

    src = tmp_path / "demo.sqlite"
    src.write_bytes(b"stubbed")
    out = tmp_path / "demo.semsql"
    vocab = tmp_path / "vocab.jsonl"
    schema_dir = tmp_path / "database_description"
    vocab.write_text("", encoding="utf-8")
    schema_dir.mkdir()

    build_graph_for_db(
        Path("target/debug/semsql.exe"),
        src,
        out,
        vocab_jsonl=vocab,
        schema_description_dir=schema_dir,
    )

    [cmd] = calls
    assert cmd[cmd.index("--vocab-jsonl") + 1] == str(vocab)
    assert cmd[cmd.index("--schema-description-dir") + 1] == str(schema_dir)


def test_build_graph_atomic_on_failure(tmp_path: Path, semsql_bin: Path) -> None:
    bogus = tmp_path / "does_not_exist.sqlite"
    out = tmp_path / "out.semsql"
    with pytest.raises(CascadeRunnerError):
        build_graph_for_db(semsql_bin, bogus, out, timeout_seconds=10)
    # No partial file left behind.
    assert not out.exists()
    assert not (out.with_suffix(out.suffix + ".tmp")).exists()


def test_query_returns_none_on_unresolvable_question(
    tmp_path: Path, semsql_bin: Path
) -> None:
    src = tmp_path / "demo.sqlite"
    _build_demo_sqlite(src)
    graph = tmp_path / "demo.semsql"
    build_graph_for_db(semsql_bin, src, graph)

    # Stage 0a can resolve "show tenants" deterministically.
    res = run_cascade_query(semsql_bin, graph, "show tenants")
    assert res.sql == "SELECT * FROM tenants"
    assert res.stage_pinned == "stage_0a"
    # Stage 0a is grammar-free, so no repair attempts.
    assert res.repair_attempts == 0

    # A genuinely complex question bails before models are wired.
    bail = run_cascade_query(
        semsql_bin, graph, "what is the average tenure of users grouped by tenant?"
    )
    assert bail.sql is None
    assert bail.stage_pinned == "needs_model"
    assert bail.repair_attempts == 0


def test_parse_repair_attempts_extracts_counter_from_stderr() -> None:
    """The ``repair_attempts=N`` tag round-trips through stderr."""
    from semsql_eval.cascade_runner import _parse_repair_attempts

    assert _parse_repair_attempts("") == 0
    assert _parse_repair_attempts("stage_pinned=stage_3\n") == 0
    assert _parse_repair_attempts("repair_attempts=0\n") == 0
    assert _parse_repair_attempts("repair_attempts=3\nstage_pinned=stage_3\n") == 3
    # Multiple — last wins (defensive against future per-stage logging).
    assert (
        _parse_repair_attempts("repair_attempts=1\nrepair_attempts=4\n") == 4
    )


def test_parse_stage3_slots_extracts_latest_json() -> None:
    from semsql_eval.cascade_runner import _parse_stage3_slots, _parse_stage_timings_us

    stderr = (
        'stage3_slots=[{"slot_name":"@field1","picked":"users.id"}]\n'
        "stage_0a=1us stage_0b=2us stage_1=3us stage_2=4us stage_3=5us stage_4=6us\n"
        "stage_pinned=stage_3\n"
    )
    assert _parse_stage3_slots(stderr) == [
        {"slot_name": "@field1", "picked": "users.id"}
    ]
    assert _parse_stage_timings_us(stderr) == {
        "stage_0a": 1,
        "stage_0b": 2,
        "stage_1": 3,
        "stage_2": 4,
        "stage_3": 5,
        "stage_4": 6,
    }
    assert _parse_stage3_slots("stage3_slots=not-json\n") == []
    assert _parse_stage_timings_us("stage_0a=nope\n") == {}


def test_compact_stderr_keeps_error_tail() -> None:
    from semsql_eval.cascade_runner import _compact_stderr

    stderr = "\n".join(f"line {idx}" for idx in range(20))
    compact = _compact_stderr(stderr)

    assert compact is not None
    assert "line 8" in compact
    assert "line 7" not in compact


def test_run_cascade_query_surfaces_error_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import semsql_eval.cascade_runner as cascade_runner

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            cmd,
            1,
            stdout="",
            stderr=(
                "stage_pinned=stage2_structural_error\n"
                "stage_0a=1us stage_0b=2us stage_1=3us "
                "stage_2=4us stage_3=5us stage_4=6us\n"
                "Error: malformed skeleton\n"
            ),
        )

    monkeypatch.setattr(cascade_runner.subprocess, "run", fake_run)

    result = run_cascade_query(
        Path("target/debug/semsql.exe"),
        tmp_path / "demo.semsql",
        "broken query",
    )

    assert result.sql is None
    assert result.stage_pinned == "stage2_structural_error"
    assert result.error_detail is not None
    assert "malformed skeleton" in result.error_detail
    assert result.elapsed_seconds >= 0.0
    assert result.stdout_bytes == 0
    assert result.stderr_bytes > 0
    assert result.stage_timings_us["stage_3"] == 5


def test_run_cascade_query_surfaces_timeout_telemetry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import semsql_eval.cascade_runner as cascade_runner

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd, timeout=kwargs["timeout"])

    monkeypatch.setattr(cascade_runner.subprocess, "run", fake_run)

    result = run_cascade_query(
        Path("target/debug/semsql.exe"),
        tmp_path / "demo.semsql",
        "slow query",
        timeout_seconds=7,
    )

    assert result.sql is None
    assert result.stage_pinned == "timeout"
    assert result.timed_out_after_seconds == 7
    assert result.elapsed_seconds >= 0.0


def test_subprocess_text_decoding_is_utf8_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import semsql_eval.cascade_runner as cascade_runner

    calls: list[dict[str, object]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(kwargs)
        if "extract" in cmd:
            output = Path(cmd[cmd.index("--output") + 1])
            output.write_text("graph", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout="SELECT 1",
            stderr="stage_pinned=stage_3\n",
        )

    monkeypatch.setattr(cascade_runner.subprocess, "run", fake_run)

    db_path = tmp_path / "demo.sqlite"
    db_path.write_bytes(b"not a real sqlite db; subprocess is stubbed")
    graph_path = tmp_path / "demo.semsql"

    build_graph_for_db(Path("target/debug/semsql.exe"), db_path, graph_path)
    result = run_cascade_query(
        Path("target/debug/semsql.exe"),
        graph_path,
        "show me the odd-byte output path",
    )

    assert result.sql == "SELECT 1"
    assert calls
    assert all(call["encoding"] == "utf-8" for call in calls)
    assert all(call["errors"] == "replace" for call in calls)


def test_build_graph_for_db_url_uses_supplied_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import semsql_eval.cascade_runner as cascade_runner

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        output = Path(cmd[cmd.index("--output") + 1])
        output.write_text("graph", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(cascade_runner.subprocess, "run", fake_run)

    out = tmp_path / "pg.semsql"
    vocab = tmp_path / "vocab.jsonl"
    schema_dir = tmp_path / "database_description"
    vocab.write_text("", encoding="utf-8")
    schema_dir.mkdir()
    build_graph_for_db_url(
        Path("target/debug/semsql.exe"),
        "postgres://user:pass@localhost/db?options=-csearch_path%3Ddemo%2Cpublic",
        out,
        vocab_jsonl=vocab,
        schema_description_dir=schema_dir,
    )

    assert out.exists()
    [cmd] = calls
    assert "postgres://user:pass@localhost/db?options=-csearch_path%3Ddemo%2Cpublic" in cmd
    assert cmd[cmd.index("--vocab-jsonl") + 1] == str(vocab)
    assert cmd[cmd.index("--schema-description-dir") + 1] == str(schema_dir)


def test_missing_onnx_failure_is_infrastructure_bucket() -> None:
    from semsql_eval.cascade_runner import _classify_failure_stage

    stderr = (
        "stage_pinned=needs_model\n"
        "Error: cascade: model stages require `--features onnx`"
    )

    assert (
        _classify_failure_stage(
            stderr,
            fallback_stage="needs_model",
            cascade_manifest=Path("manifest.json"),
            returncode=1,
        )
        == "missing_onnx_feature"
    )
    assert (
        _classify_failure_stage(
            stderr,
            fallback_stage="needs_model",
            cascade_manifest=None,
            returncode=1,
        )
        == "needs_model"
    )


def test_cascade_env_pins_repo_local_ort_dylib(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from semsql_eval.cascade_runner import _cascade_subprocess_env

    monkeypatch.delenv("ORT_DYLIB_PATH", raising=False)
    dll = tmp_path / "target" / "release" / "onnxruntime.dll"
    dll.parent.mkdir(parents=True)
    dll.write_bytes(b"fake")

    env = _cascade_subprocess_env(tmp_path / "target" / "debug" / "semsql.exe")

    assert env["ORT_DYLIB_PATH"] == str(dll.resolve())


def test_cascade_env_preserves_explicit_ort_dylib(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from semsql_eval.cascade_runner import _cascade_subprocess_env

    explicit = tmp_path / "custom" / "onnxruntime.dll"
    monkeypatch.setenv("ORT_DYLIB_PATH", str(explicit))

    env = _cascade_subprocess_env(tmp_path / "target" / "debug" / "semsql.exe")

    assert env["ORT_DYLIB_PATH"] == str(explicit)


def test_make_predictor_returns_sentinel_on_bail(
    tmp_path: Path, semsql_bin: Path
) -> None:
    src = tmp_path / "demo.sqlite"
    _build_demo_sqlite(src)

    predict = make_cascade_predictor(
        semsql_bin=semsql_bin,
        graph_cache_dir=tmp_path / "graphs",
        sentinel_sql="SELECT 0",
    )
    ex_easy = Example(
        db_id="demo",
        question="show tenants",
        gold_sql="SELECT * FROM tenants",
        db_path=src,
    )
    ex_hard = Example(
        db_id="demo",
        question="cross-tenant migration of all expired sessions",
        gold_sql="SELECT 1",
        db_path=src,
    )
    assert predict(ex_easy) == "SELECT * FROM tenants"
    assert predict(ex_hard) == "SELECT 0"


def test_make_predictor_caches_graph_per_db(
    tmp_path: Path, semsql_bin: Path
) -> None:
    src = tmp_path / "demo.sqlite"
    _build_demo_sqlite(src)
    cache = tmp_path / "graphs"

    predict = make_cascade_predictor(semsql_bin=semsql_bin, graph_cache_dir=cache)
    ex = Example(db_id="demo", question="show tenants", gold_sql="x", db_path=src)
    predict(ex)
    first = (cache / "demo.semsql").stat().st_mtime_ns
    predict(ex)
    second = (cache / "demo.semsql").stat().st_mtime_ns
    assert first == second, "graph file should be reused across calls for the same db_id"


def test_predictor_raises_when_binary_missing(tmp_path: Path) -> None:
    with pytest.raises(CascadeRunnerError, match="semsql binary not found"):
        make_cascade_predictor(
            semsql_bin=tmp_path / "nope.exe",
            graph_cache_dir=tmp_path / "graphs",
        )


def test_run_cascade_query_forwards_intent_yaml(
    tmp_path: Path, semsql_bin: Path
) -> None:
    """The runner should pass `--intent-yaml` through so Stage 0b's
    Top-N pattern can use the production intent library at eval time."""
    src = tmp_path / "demo.sqlite"
    _build_demo_sqlite(src)
    graph = tmp_path / "demo.semsql"
    build_graph_for_db(semsql_bin, src, graph)

    intent = tmp_path / "patterns.yaml"
    intent.write_text(
        "- pattern: '\\b(top|highest)\\b'\n"
        "  intent_type: ranking\n"
        "  column_hints: [id]\n"
        "  ordering: DESC\n"
        "  default_limit: 5\n",
        encoding="utf-8",
    )

    # The intent path itself must be accepted by the binary — assert no
    # crash and that a Stage 0a query still resolves through.
    res = run_cascade_query(
        semsql_bin, graph, "show tenants", intent_yaml=intent
    )
    assert res.sql == "SELECT * FROM tenants"
    assert res.stage_pinned == "stage_0a"


def test_run_cascade_query_forwards_cascade_manifest_arg(
    tmp_path: Path, semsql_bin: Path
) -> None:
    """The runner should pass `--cascade-manifest` through.

    Non-ONNX builds ignore the placeholder manifest and resolve through
    Stage 0a. ONNX-enabled debug builds attempt to load it and fail fast,
    which still proves the flag reached the binary.
    """
    src = tmp_path / "demo.sqlite"
    _build_demo_sqlite(src)
    graph = tmp_path / "demo.semsql"
    build_graph_for_db(semsql_bin, src, graph)

    # Manifest doesn't need to be valid JSON in non-onnx builds — the
    # binary never reads it. We point at a placeholder so the flag is
    # forwarded.
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")

    res = run_cascade_query(
        semsql_bin, graph, "show tenants", cascade_manifest=manifest
    )
    assert res.sql == "SELECT * FROM tenants" or res.stage_pinned == "error"


def test_run_cascade_query_forwards_oracle_args(
    tmp_path: Path, semsql_bin: Path
) -> None:
    """Oracle flags are diagnostic-only, but the subprocess wrapper must
    still pass them through without perturbing deterministic Stage 0a hits."""
    src = tmp_path / "demo.sqlite"
    _build_demo_sqlite(src)
    graph = tmp_path / "demo.semsql"
    build_graph_for_db(semsql_bin, src, graph)

    res = run_cascade_query(
        semsql_bin,
        graph,
        "show tenants",
        oracle_skeleton="SELECT @field1 FROM @entity1",
        oracle_schema_json='{"entities":["tenants"],"fields":["tenants.id"],"top_score":1.0}',
        oracle_slots_json='{"@entity1":"tenants","@field1":"tenants.id"}',
    )
    assert res.sql == "SELECT * FROM tenants"
    assert res.stage_pinned == "stage_0a"


def test_run_cascade_query_loads_query_frame_json(
    tmp_path: Path, semsql_bin: Path
) -> None:
    src = tmp_path / "demo.sqlite"
    _build_demo_sqlite(src)
    graph = tmp_path / "demo.semsql"
    build_graph_for_db(semsql_bin, src, graph)

    frame_path = tmp_path / "frames" / "one.json"
    res = run_cascade_query(
        semsql_bin,
        graph,
        "show tenants",
        query_frame_json=frame_path,
    )

    assert res.sql == "SELECT * FROM tenants"
    assert frame_path.exists()
    assert res.query_frame is not None
    assert res.query_frame["source"] == "query_frame"
    assert res.query_frame["pre_stage3"] is None


def test_predictor_rejects_path_traversal_db_ids(
    tmp_path: Path, semsql_bin: Path
) -> None:
    src = tmp_path / "demo.sqlite"
    _build_demo_sqlite(src)
    predict = make_cascade_predictor(
        semsql_bin=semsql_bin, graph_cache_dir=tmp_path / "graphs"
    )
    hostile_inputs = (
        "../etc",
        "..\\evil",
        "foo/bar",
        "foo\\bar",
        ".",
        "..",
        ".hidden",       # leading dot — could shadow a unix dotfile
        "C:malicious",   # drive-letter abuse on Windows
        "ok\x00../bad",  # NUL-byte truncation
        "",              # empty
    )
    for hostile in hostile_inputs:
        ex = Example(db_id=hostile, question="x", gold_sql="y", db_path=src)
        with pytest.raises(CascadeRunnerError):
            predict(ex)
