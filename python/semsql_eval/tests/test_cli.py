"""Smoke tests for the `semsql-eval` CLI runner."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PYTHONPATH = os.pathsep.join(
    [
        str(_REPO_ROOT / "python" / "semsql_eval" / "src"),
        str(_REPO_ROOT / "python" / "semsql_rewriter" / "src"),
    ]
)


def _subprocess_env() -> dict[str, str]:
    """Inject PYTHONPATH so `python -m semsql_eval` resolves the
    workspace package without an editable install. Mirrors the
    conftest hack used for in-process imports."""
    env = os.environ.copy()
    env["PYTHONPATH"] = (
        _PYTHONPATH + os.pathsep + env["PYTHONPATH"]
        if "PYTHONPATH" in env
        else _PYTHONPATH
    )
    return env


def _semsql_binary() -> Path | None:
    for c in (
        Path("target/debug/semsql.exe"),
        Path("target/debug/semsql"),
        Path("target/release/semsql.exe"),
        Path("target/release/semsql"),
    ):
        if c.exists():
            return c.resolve()
    found = shutil.which("semsql")
    return Path(found) if found else None


@pytest.fixture()
def semsql_bin() -> Path:
    bin_path = _semsql_binary()
    if bin_path is None:
        pytest.skip("semsql binary not available — build with `cargo build -p semsql-cli`")
    return bin_path


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE tenants (id INTEGER PRIMARY KEY, name TEXT);
        INSERT INTO tenants VALUES (1, 'Acme'), (2, 'Globex');
        """
    )
    conn.close()


def test_spider_cli_runs_and_reports_exec_acc(
    tmp_path: Path, semsql_bin: Path
) -> None:
    # Build a tiny one-DB Spider-shaped fixture.
    db_root = tmp_path / "database"
    db_dir = db_root / "demo"
    db_dir.mkdir(parents=True)
    db = db_dir / "demo.sqlite"
    _make_db(db)

    questions = tmp_path / "dev.json"
    questions.write_text(
        json.dumps(
            [
                {
                    "db_id": "demo",
                    "question": "show tenants",
                    "query": "SELECT * FROM tenants",
                },
                {
                    "db_id": "demo",
                    "question": "average tenure of all customers grouped by region",
                    "query": "SELECT 1",
                },
            ]
        ),
        encoding="utf-8",
    )

    report = tmp_path / "report.json"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "semsql_eval",
            "spider",
            "--questions",
            str(questions),
            "--db-root",
            str(db_root),
            "--semsql-bin",
            str(semsql_bin),
            "--graph-cache-dir",
            str(tmp_path / "graphs"),
            "--report-json",
            str(report),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_subprocess_env(),
    )
    assert proc.returncode == 0, proc.stderr
    assert "exec_acc=" in proc.stdout
    assert "total=2" in proc.stdout
    assert report.exists()
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["summary"]["total"] == 2
    # The pre-resolver handles "show tenants"; the second query bails
    # to the sentinel "SELECT 1". As of the bailed-bucket change, the
    # bail does NOT credit toward `correct` even when the gold also
    # evaluates to the sentinel — this distinguishes "cascade
    # committed and was right" from "cascade gave up". So exec_acc =
    # 1/2 = 0.5, with bailed = 1.
    assert data["summary"]["total"] == 2
    assert data["summary"]["correct"] == 1
    assert data["summary"]["bailed"] == 1
    assert data["summary"]["exec_acc"] == 0.5
    assert data["summary"]["bail_rate"] == 0.5
    # Stage breakdown: one example pinned at Stage 0a, the other at
    # `needs_model` because it's a complex question Stage 1+ would
    # handle.
    assert data["summary"]["stage_breakdown"]["stage_0a"] == 1
    assert data["summary"]["stage_breakdown"]["needs_model"] == 1
    # Per-example records also carry the stage tag for drill-down.
    pinned_set = {r["stage_pinned"] for r in data["examples"]}
    assert pinned_set == {"stage_0a", "needs_model"}
    # Plain-text summary surfaces the per-stage breakdown line.
    assert "stages:" in proc.stdout


def test_bypass_corpus_cli_lists_cases(tmp_path: Path) -> None:
    # Synthesise a minimal corpus rather than relying on the committed
    # fixture path — keeps the test hermetic.
    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text(
        "\n".join(
            [
                json.dumps({"name": "case_a", "should_pass": True}),
                json.dumps({"name": "case_b", "should_pass": False}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "semsql_eval",
            "bypass-corpus",
            "--corpus",
            str(corpus),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_subprocess_env(),
    )
    assert proc.returncode == 0, proc.stderr
    assert "positive=1" in proc.stdout
    assert "negative=1" in proc.stdout
    assert "case_a" in proc.stdout
    assert "case_b" in proc.stdout


def _make_spider_layout(root: Path, db_id: str = "demo") -> None:
    """Build a minimal valid Spider 1.0 layout under `root`."""
    (root / "database" / db_id).mkdir(parents=True, exist_ok=True)
    sqlite_path = root / "database" / db_id / f"{db_id}.sqlite"
    conn = sqlite3.connect(sqlite_path)
    try:
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.commit()
    finally:
        conn.close()
    (root / "dev.json").write_text(
        json.dumps([{"db_id": db_id, "question": "show t", "query": "SELECT 1"}]),
        encoding="utf-8",
    )
    (root / "tables.json").write_text(json.dumps([]), encoding="utf-8")


def test_check_spider_reports_healthy_layout(tmp_path: Path) -> None:
    _make_spider_layout(tmp_path / "spider")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "semsql_eval",
            "check-spider",
            "--root",
            str(tmp_path / "spider"),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_subprocess_env(),
    )
    assert proc.returncode == 0, proc.stderr
    assert "layout looks healthy" in proc.stdout
    assert "ok: dev.json" in proc.stdout


def test_check_spider_flags_missing_sqlite(tmp_path: Path) -> None:
    _make_spider_layout(tmp_path / "spider")
    # Remove the SQLite to break the layout.
    (tmp_path / "spider" / "database" / "demo" / "demo.sqlite").unlink()
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "semsql_eval",
            "check-spider",
            "--root",
            str(tmp_path / "spider"),
            "--strict",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_subprocess_env(),
    )
    assert proc.returncode == 1
    assert "demo.sqlite is missing" in proc.stderr


def test_check_spider_flags_dangling_db_id_reference(tmp_path: Path) -> None:
    _make_spider_layout(tmp_path / "spider")
    # Add a dev.json reference to a db_id that doesn't exist on disk.
    (tmp_path / "spider" / "dev.json").write_text(
        json.dumps(
            [
                {"db_id": "demo", "question": "x", "query": "SELECT 1"},
                {"db_id": "missing_db", "question": "y", "query": "SELECT 2"},
            ]
        ),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "semsql_eval",
            "check-spider",
            "--root",
            str(tmp_path / "spider"),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_subprocess_env(),
    )
    # Without --strict, exit 0 even when issues are found.
    assert proc.returncode == 0
    assert "missing_db" in proc.stderr


def test_build_mini_corpus_cli_writes_layout(tmp_path: Path) -> None:
    out = tmp_path / "mini"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "semsql_eval",
            "build-mini-corpus",
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_subprocess_env(),
    )
    assert proc.returncode == 0, proc.stderr
    assert "mini-corpus written" in proc.stdout
    assert (out / "dev.json").exists()
    assert (out / "tables.json").exists()
    assert (out / "database").is_dir()
    # The corpus is itself layout-healthy per `check-spider`.
    check = subprocess.run(
        [
            sys.executable,
            "-m",
            "semsql_eval",
            "check-spider",
            "--root",
            str(out),
            "--strict",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_subprocess_env(),
    )
    assert check.returncode == 0, check.stderr
    assert "layout looks healthy" in check.stdout


def test_query_dialect_flag_renders_per_dialect(
    tmp_path: Path, semsql_bin: Path
) -> None:
    """`semsql query --dialect <name>` should re-render the cascade
    output per dialect — backticks for MySQL, double quotes for
    Postgres / SQLite. Smoke covers the round-trip; per-dialect
    quirks are unit-tested in the renderer crate."""
    db_dir = tmp_path / "database" / "demo"
    db_dir.mkdir(parents=True)
    db = db_dir / "demo.sqlite"
    _make_db(db)

    graph = tmp_path / "demo.semsql"
    extract = subprocess.run(
        [
            str(semsql_bin),
            "extract",
            str(tmp_path),
            "--framework",
            "none",
            "--db-url",
            f"sqlite:{db.resolve()}",
            "--output",
            str(graph),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert extract.returncode == 0, extract.stderr

    # Default — dialect-agnostic output, unquoted identifiers.
    default = subprocess.run(
        [str(semsql_bin), "query", "--graph", str(graph), "show tenants"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert default.returncode == 0, default.stderr
    assert "FROM tenants" in default.stdout

    # MySQL — backtick-quoted identifiers.
    mysql = subprocess.run(
        [
            str(semsql_bin),
            "query",
            "--graph",
            str(graph),
            "show tenants",
            "--dialect",
            "mysql",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert mysql.returncode == 0, mysql.stderr
    assert "FROM `tenants`" in mysql.stdout

    # SQLite — double-quoted identifiers.
    sqlite = subprocess.run(
        [
            str(semsql_bin),
            "query",
            "--graph",
            str(graph),
            "show tenants",
            "--dialect",
            "sqlite",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert sqlite.returncode == 0, sqlite.stderr
    assert 'FROM "tenants"' in sqlite.stdout

    # MSSQL — square-bracket identifiers + TOP n (no LIMIT clause).
    mssql = subprocess.run(
        [
            str(semsql_bin),
            "query",
            "--graph",
            str(graph),
            "show tenants",
            "--dialect",
            "mssql",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert mssql.returncode == 0, mssql.stderr
    assert "FROM [tenants]" in mssql.stdout

    # BigQuery — backticks (NOT double quotes — those are strings in BQ).
    bq = subprocess.run(
        [
            str(semsql_bin),
            "query",
            "--graph",
            str(graph),
            "show tenants",
            "--dialect",
            "bigquery",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert bq.returncode == 0, bq.stderr
    assert "FROM `tenants`" in bq.stdout
    assert '"tenants"' not in bq.stdout


def test_query_dialect_flag_rejects_unknown_dialect(
    tmp_path: Path, semsql_bin: Path
) -> None:
    db_dir = tmp_path / "database" / "demo"
    db_dir.mkdir(parents=True)
    db = db_dir / "demo.sqlite"
    _make_db(db)
    graph = tmp_path / "demo.semsql"
    subprocess.run(
        [
            str(semsql_bin),
            "extract",
            str(tmp_path),
            "--framework",
            "none",
            "--db-url",
            f"sqlite:{db.resolve()}",
            "--output",
            str(graph),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    proc = subprocess.run(
        [
            str(semsql_bin),
            "query",
            "--graph",
            str(graph),
            "show tenants",
            "--dialect",
            "oracle",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0
    assert "unknown dialect `oracle`" in proc.stderr


def test_doctor_surfaces_eval_report_stage_breakdown(
    tmp_path: Path, semsql_bin: Path
) -> None:
    """`semsql doctor --eval-report` should print the per-stage breakdown
    block emitted by the eval CLI's `--report-json`. Exercises the
    full surface: extract → eval-style JSON → doctor reads + renders.

    We fabricate a minimal report instead of running the eval to keep
    the test fast and deterministic — the contract tested here is the
    JSON shape, which the eval CLI also writes (covered by
    test_spider_cli_runs_and_reports_exec_acc above)."""
    db_dir = tmp_path / "database" / "demo"
    db_dir.mkdir(parents=True)
    db = db_dir / "demo.sqlite"
    _make_db(db)

    # Materialise a `.semsql` so doctor has something to read.
    graph = tmp_path / "demo.semsql"
    extract = subprocess.run(
        [
            str(semsql_bin),
            "extract",
            str(tmp_path),
            "--framework",
            "none",
            "--db-url",
            f"sqlite:{db.resolve()}",
            "--output",
            str(graph),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert extract.returncode == 0, extract.stderr

    # Fabricate a healthy eval report — full stage_0a coverage so doctor
    # exits 0.
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "summary": {
                    "suite": "spider",
                    "total": 4,
                    "correct": 4,
                    "wrong": 0,
                    "bailed": 0,
                    "errored": 0,
                    "exec_acc": 1.0,
                    "bail_rate": 0.0,
                    "error_rate": 0.0,
                    "stage_breakdown": {"stage_0a": 4},
                },
                "examples": [],
            }
        ),
        encoding="utf-8",
    )

    doctor = subprocess.run(
        [
            str(semsql_bin),
            "doctor",
            "--graph",
            str(graph),
            "--eval-report",
            str(report),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert doctor.returncode == 0, doctor.stderr
    assert "eval report:" in doctor.stdout
    assert "stage_breakdown:" in doctor.stdout
    assert "stage_0a" in doctor.stdout
    # Healthy eval coverage — no cascade-coverage warning. (Other
    # warnings, like "entities lack UI-layer vocabulary", are
    # surfaced by the graph-level doctor checks and unrelated to the
    # eval-report contract under test.)
    assert "cascade coverage" not in doctor.stdout
    assert "errored or timed out" not in doctor.stdout


def test_doctor_examples_drills_into_non_correct_records(
    tmp_path: Path, semsql_bin: Path
) -> None:
    """`semsql doctor --eval-report ... --examples N` should print the
    first N non-correct examples (bailed/errored/timeout) so an
    operator can drill from the per-stage breakdown straight to a
    concrete query/SQL pair without re-running the eval."""
    db_dir = tmp_path / "database" / "demo"
    db_dir.mkdir(parents=True)
    db = db_dir / "demo.sqlite"
    _make_db(db)

    graph = tmp_path / "demo.semsql"
    extract = subprocess.run(
        [
            str(semsql_bin),
            "extract",
            str(tmp_path),
            "--framework",
            "none",
            "--db-url",
            f"sqlite:{db.resolve()}",
            "--output",
            str(graph),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert extract.returncode == 0, extract.stderr

    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "summary": {
                    "suite": "spider",
                    "total": 4,
                    "correct": 1,
                    "wrong": 0,
                    "bailed": 3,
                    "errored": 0,
                    "exec_acc": 0.25,
                    "bail_rate": 0.75,
                    "error_rate": 0.0,
                    "stage_breakdown": {"stage_0a": 1, "needs_model": 3},
                },
                "examples": [
                    {
                        "db_id": "demo",
                        "question": "show tenants",
                        "gold_sql": "SELECT * FROM tenants",
                        "pred_sql": "SELECT * FROM tenants",
                        "stage_pinned": "stage_0a",
                    },
                    {
                        "db_id": "demo",
                        "question": "complex aggregation example one",
                        "gold_sql": "SELECT COUNT(*) FROM tenants",
                        "pred_sql": "SELECT 1",
                        "stage_pinned": "needs_model",
                    },
                    {
                        "db_id": "demo",
                        "question": "complex aggregation example two",
                        "gold_sql": "SELECT name FROM tenants",
                        "pred_sql": "SELECT 1",
                        "stage_pinned": "needs_model",
                    },
                    {
                        "db_id": "demo",
                        "question": "complex aggregation example three",
                        "gold_sql": "SELECT name FROM tenants",
                        "pred_sql": "SELECT 1",
                        "stage_pinned": "needs_model",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    proc = subprocess.run(
        [
            str(semsql_bin),
            "doctor",
            "--graph",
            str(graph),
            "--eval-report",
            str(report),
            "--examples",
            "2",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    # Low-coverage report → non-zero exit, but the drilldown text
    # still renders to stdout.
    assert "eval drilldown" in proc.stdout
    assert "complex aggregation example one" in proc.stdout
    assert "complex aggregation example two" in proc.stdout
    # Cap honoured — only 2 surfaced even though 3 non-correct exist.
    assert "complex aggregation example three" not in proc.stdout


def test_doctor_write_overrides_creates_yaml_scaffold(
    tmp_path: Path, semsql_bin: Path
) -> None:
    """`semsql doctor --write-overrides path` should emit a starter
    YAML even when there are no conflicts in the graph — it's the
    expected workflow for users to bootstrap an overrides file."""
    db_dir = tmp_path / "database" / "demo"
    db_dir.mkdir(parents=True)
    db = db_dir / "demo.sqlite"
    _make_db(db)

    graph = tmp_path / "demo.semsql"
    subprocess.run(
        [
            str(semsql_bin),
            "extract",
            str(tmp_path),
            "--framework",
            "none",
            "--db-url",
            f"sqlite:{db.resolve()}",
            "--output",
            str(graph),
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    out = tmp_path / "semsql.overrides.yaml"
    proc = subprocess.run(
        [
            str(semsql_bin),
            "doctor",
            "--graph",
            str(graph),
            "--write-overrides",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.exists()
    body = out.read_text(encoding="utf-8")
    assert "version: 1" in body
    assert "overrides:" in body
    assert "wrote 0 conflict scaffold(s)" in proc.stdout


def test_doctor_write_overrides_refuses_existing_file(
    tmp_path: Path, semsql_bin: Path
) -> None:
    db_dir = tmp_path / "database" / "demo"
    db_dir.mkdir(parents=True)
    db = db_dir / "demo.sqlite"
    _make_db(db)

    graph = tmp_path / "demo.semsql"
    subprocess.run(
        [
            str(semsql_bin),
            "extract",
            str(tmp_path),
            "--framework",
            "none",
            "--db-url",
            f"sqlite:{db.resolve()}",
            "--output",
            str(graph),
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    out = tmp_path / "semsql.overrides.yaml"
    out.write_text("existing content\n", encoding="utf-8")
    proc = subprocess.run(
        [
            str(semsql_bin),
            "doctor",
            "--graph",
            str(graph),
            "--write-overrides",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0
    assert "refusing to overwrite" in proc.stderr or "refusing to overwrite" in proc.stdout
    # Existing content preserved.
    assert out.read_text(encoding="utf-8") == "existing content\n"


def test_doctor_format_json_emits_machine_readable_payload(
    tmp_path: Path, semsql_bin: Path
) -> None:
    """`semsql doctor --format json` emits a single JSON document
    with every diagnostic the text path surfaces. Suitable for CI
    parsing — sample test asserts the top-level shape."""
    db_dir = tmp_path / "database" / "demo"
    db_dir.mkdir(parents=True)
    db = db_dir / "demo.sqlite"
    _make_db(db)

    graph = tmp_path / "demo.semsql"
    subprocess.run(
        [
            str(semsql_bin),
            "extract",
            str(tmp_path),
            "--framework",
            "none",
            "--db-url",
            f"sqlite:{db.resolve()}",
            "--output",
            str(graph),
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    proc = subprocess.run(
        [
            str(semsql_bin),
            "doctor",
            "--graph",
            str(graph),
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert "graph" in payload
    assert "coverage" in payload
    assert "conflicts" in payload
    assert "rls" in payload
    assert "eval_report" in payload
    assert "exit_nonzero" in payload
    assert payload["exit_nonzero"] is False
    assert payload["coverage"]["entities"] >= 1
    assert payload["rls"]["status"] == "skipped"
    assert isinstance(payload["conflicts"], list)


def test_doctor_format_json_rejects_unknown_format(
    tmp_path: Path, semsql_bin: Path
) -> None:
    db_dir = tmp_path / "database" / "demo"
    db_dir.mkdir(parents=True)
    db = db_dir / "demo.sqlite"
    _make_db(db)
    graph = tmp_path / "demo.semsql"
    subprocess.run(
        [
            str(semsql_bin),
            "extract",
            str(tmp_path),
            "--framework",
            "none",
            "--db-url",
            f"sqlite:{db.resolve()}",
            "--output",
            str(graph),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    proc = subprocess.run(
        [
            str(semsql_bin),
            "doctor",
            "--graph",
            str(graph),
            "--format",
            "yaml",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0
    assert "unknown --format `yaml`" in proc.stderr or "yaml" in proc.stderr


def test_doctor_rls_strict_fails_when_scoped_entity_unverified(
    tmp_path: Path, semsql_bin: Path
) -> None:
    """`--rls-strict` must fail-closed when graph declares scoped
    entities AND no `--db-url` is supplied to verify their RLS
    posture. We manually insert a `scopes` row via sqlite3 since the
    `--framework=none` extractor doesn't auto-detect tenanted
    tables — that's a manual config step in real deployments."""
    db_dir = tmp_path / "database" / "demo"
    db_dir.mkdir(parents=True)
    db = db_dir / "demo.sqlite"
    _make_db(db)
    graph = tmp_path / "demo.semsql"
    subprocess.run(
        [
            str(semsql_bin),
            "extract",
            str(tmp_path),
            "--framework",
            "none",
            "--db-url",
            f"sqlite:{db.resolve()}",
            "--output",
            str(graph),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    # Inject a scope row so coverage.scoped_entities is non-empty.
    conn = sqlite3.connect(graph)
    try:
        conn.execute(
            "INSERT INTO scopes(entity, kind, template, required_params, source_rule) "
            "VALUES (?, ?, ?, ?, ?)",
            ("tenants", "tenant", "tenant_id = :tenant", "[\"tenant\"]", "test_fixture"),
        )
        conn.commit()
    finally:
        conn.close()

    # No --db-url → unverified → strict mode must fail.
    proc = subprocess.run(
        [
            str(semsql_bin),
            "doctor",
            "--graph",
            str(graph),
            "--rls-strict",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1, proc.stdout
    assert "rls-strict" in proc.stdout
    assert "RLS posture was not verified" in proc.stdout

    # Without --rls-strict, same graph reports the warning but exits 0.
    proc2 = subprocess.run(
        [
            str(semsql_bin),
            "doctor",
            "--graph",
            str(graph),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc2.returncode == 0, proc2.stderr


def test_doctor_eval_report_low_coverage_lifts_exit_code(
    tmp_path: Path, semsql_bin: Path
) -> None:
    """A report with <50% Stage 0a coverage must surface a warning AND
    cause `doctor` to exit non-zero — that's the deployment-blocking
    contract CI relies on."""
    db_dir = tmp_path / "database" / "demo"
    db_dir.mkdir(parents=True)
    db = db_dir / "demo.sqlite"
    _make_db(db)

    graph = tmp_path / "demo.semsql"
    extract = subprocess.run(
        [
            str(semsql_bin),
            "extract",
            str(tmp_path),
            "--framework",
            "none",
            "--db-url",
            f"sqlite:{db.resolve()}",
            "--output",
            str(graph),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert extract.returncode == 0, extract.stderr

    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "summary": {
                    "suite": "spider",
                    "total": 10,
                    "correct": 1,
                    "wrong": 0,
                    "bailed": 9,
                    "errored": 0,
                    "exec_acc": 0.1,
                    "bail_rate": 0.9,
                    "error_rate": 0.0,
                    "stage_breakdown": {"stage_0a": 1, "needs_model": 9},
                },
                "examples": [],
            }
        ),
        encoding="utf-8",
    )

    doctor = subprocess.run(
        [
            str(semsql_bin),
            "doctor",
            "--graph",
            str(graph),
            "--eval-report",
            str(report),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert doctor.returncode == 1, (
        f"expected non-zero exit on low-coverage report; got {doctor.returncode}\n"
        f"stdout: {doctor.stdout}\nstderr: {doctor.stderr}"
    )
    assert "cascade coverage (Stage 0a) is 10.0%" in doctor.stdout
    assert "below the 50% deployment threshold" in doctor.stdout


def _make_valid_manifest(tmp_path: Path) -> Path:
    """Materialise a valid cascade manifest + the ONNX/tokenizer files
    it references. Files are zero-byte stand-ins — `CascadeManifest::load`
    only checks existence, not validity, so this is sufficient for the
    doctor path."""
    for name in (
        "linker.onnx",
        "linker.tok.json",
        "skeleton.onnx",
        "skeleton.tok.json",
        "slot.onnx",
        "slot.tok.json",
    ):
        (tmp_path / name).write_bytes(b"")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "cascade_version": "v0.5.0",
                "linker": {
                    "path": "linker.onnx",
                    "tokenizer": "linker.tok.json",
                    "params": 9_500_000,
                },
                "skeleton": {
                    "path": "skeleton.onnx",
                    "tokenizer": "skeleton.tok.json",
                    "params": 19_800_000,
                },
                "slot_filler": {
                    "path": "slot.onnx",
                    "tokenizer": "slot.tok.json",
                    "params": 4_900_000,
                },
            }
        ),
        encoding="utf-8",
    )
    return manifest


def test_doctor_cascade_manifest_warns_on_non_onnx_build(
    tmp_path: Path, semsql_bin: Path
) -> None:
    """Doctor should validate a well-formed manifest and warn when the
    binary was built without `--features onnx` (default in CI)."""
    db_dir = tmp_path / "database" / "demo"
    db_dir.mkdir(parents=True)
    db = db_dir / "demo.sqlite"
    _make_db(db)
    graph = tmp_path / "demo.semsql"
    subprocess.run(
        [
            str(semsql_bin),
            "extract",
            str(tmp_path),
            "--framework",
            "none",
            "--db-url",
            f"sqlite:{db.resolve()}",
            "--output",
            str(graph),
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    manifest = _make_valid_manifest(tmp_path)
    proc = subprocess.run(
        [
            str(semsql_bin),
            "doctor",
            "--graph",
            str(graph),
            "--cascade-manifest",
            str(manifest),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    # Manifest validates fine — header line present.
    assert "cascade manifest:" in proc.stdout
    assert "cascade_version=v0.5.0" in proc.stdout
    # Default test build is non-onnx → doctor emits the warning AND
    # exits non-zero so CI catches the misconfig.
    assert "WITHOUT `--features onnx`" in proc.stdout
    assert proc.returncode == 1


def test_doctor_cascade_manifest_rejects_missing_artefact(
    tmp_path: Path, semsql_bin: Path
) -> None:
    """A manifest pointing at a missing ONNX file must fail closed."""
    db_dir = tmp_path / "database" / "demo"
    db_dir.mkdir(parents=True)
    db = db_dir / "demo.sqlite"
    _make_db(db)
    graph = tmp_path / "demo.semsql"
    subprocess.run(
        [
            str(semsql_bin),
            "extract",
            str(tmp_path),
            "--framework",
            "none",
            "--db-url",
            f"sqlite:{db.resolve()}",
            "--output",
            str(graph),
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    # Manifest references files that don't exist.
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "cascade_version": "v0",
                "linker": {"path": "x.onnx", "tokenizer": "x.tok", "params": 0},
                "skeleton": {"path": "y.onnx", "tokenizer": "y.tok", "params": 0},
                "slot_filler": {"path": "z.onnx", "tokenizer": "z.tok", "params": 0},
            }
        ),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [
            str(semsql_bin),
            "doctor",
            "--graph",
            str(graph),
            "--cascade-manifest",
            str(bad),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    assert "load failed" in proc.stdout
    assert "missing linker ONNX" in proc.stdout


def test_doctor_cascade_manifest_in_json_format(
    tmp_path: Path, semsql_bin: Path
) -> None:
    """JSON doctor surfaces the manifest block under `cascade_manifest`."""
    db_dir = tmp_path / "database" / "demo"
    db_dir.mkdir(parents=True)
    db = db_dir / "demo.sqlite"
    _make_db(db)
    graph = tmp_path / "demo.semsql"
    subprocess.run(
        [
            str(semsql_bin),
            "extract",
            str(tmp_path),
            "--framework",
            "none",
            "--db-url",
            f"sqlite:{db.resolve()}",
            "--output",
            str(graph),
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    manifest = _make_valid_manifest(tmp_path)
    proc = subprocess.run(
        [
            str(semsql_bin),
            "doctor",
            "--graph",
            str(graph),
            "--cascade-manifest",
            str(manifest),
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    payload = json.loads(proc.stdout)
    assert payload["cascade_manifest"] is not None
    block = payload["cascade_manifest"]
    assert block["ok"] is True
    assert block["cascade_version"] == "v0.5.0"
    assert block["onnx_build"] is False
    assert payload["exit_nonzero"] is True


def test_check_spider_flags_missing_manifests(tmp_path: Path) -> None:
    (tmp_path / "spider").mkdir()
    (tmp_path / "spider" / "database").mkdir()
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "semsql_eval",
            "check-spider",
            "--root",
            str(tmp_path / "spider"),
            "--strict",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_subprocess_env(),
    )
    assert proc.returncode == 1
    assert "dev.json" in proc.stderr
    assert "tables.json" in proc.stderr
