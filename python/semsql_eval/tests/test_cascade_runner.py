"""Tests for `cascade_runner` — exercise the subprocess wrapper end-to-end
against the real `semsql` binary when it's available, and the bail
paths when it isn't."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from semsql_eval.cascade_runner import (
    CascadeRunnerError,
    build_graph_for_db,
    make_cascade_predictor,
    run_cascade_query,
)
from semsql_eval.spider import Example


def _semsql_binary() -> Path | None:
    """Locate the freshly-built semsql binary in cargo target/, falling
    back to PATH. Returns None if neither is available — tests that
    require it skip gracefully."""
    candidates = [
        Path("target/debug/semsql.exe"),
        Path("target/debug/semsql"),
        Path("target/release/semsql.exe"),
        Path("target/release/semsql"),
    ]
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


@pytest.fixture()
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

    # A genuinely complex question bails before models are wired.
    bail = run_cascade_query(
        semsql_bin, graph, "what is the average tenure of users grouped by tenant?"
    )
    assert bail.sql is None
    assert bail.stage_pinned == "needs_model"


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
