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
