"""Smoke tests for the `semsql-eval` CLI runner."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from click.testing import CliRunner
from semsql_eval.__main__ import (
    _grouped_avg_sql_mentions_expected_fields,
    _realdb_typed_fallback_packet_schema_evidence,
    _selected_sql_matches_typed_fallback_question,
    _summarize_realdb_packet_schema_evidence,
    _summarize_realdb_typed_fallback_records,
    _typed_provider_readiness,
)

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
    explicit = os.environ.get("SEMSQL_BIN")
    if explicit:
        return Path(explicit).resolve()
    cargo = shutil.which("cargo")
    if cargo is not None:
        subprocess.run(
            [cargo, "build", "-p", "semsql-cli"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
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


@pytest.fixture(scope="session")
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


def _make_semantic_graph(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE semsql_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO semsql_metadata VALUES ('schema_version', '1');

        CREATE TABLE entities (
            canonical_name TEXT PRIMARY KEY,
            db_table TEXT NOT NULL,
            db_schema TEXT,
            singular_label TEXT,
            plural_label TEXT,
            proto_blob BLOB NOT NULL DEFAULT X''
        );
        CREATE TABLE fields (
            entity TEXT NOT NULL,
            field TEXT NOT NULL,
            db_column TEXT NOT NULL,
            type TEXT NOT NULL,
            display_label TEXT,
            enum_canonical TEXT,
            unit_canonical TEXT,
            proto_blob BLOB NOT NULL DEFAULT X'',
            PRIMARY KEY (entity, field)
        );
        CREATE TABLE relationships (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_entity TEXT NOT NULL,
            from_field TEXT NOT NULL,
            to_entity TEXT NOT NULL,
            to_field TEXT NOT NULL,
            kind TEXT NOT NULL,
            relation_name TEXT,
            proto_blob BLOB NOT NULL DEFAULT X''
        );
        CREATE TABLE sample_values (
            field_canonical TEXT PRIMARY KEY,
            examples TEXT NOT NULL,
            pii_redacted INTEGER NOT NULL DEFAULT 0
        );

        INSERT INTO entities(canonical_name, db_table, singular_label, plural_label)
        VALUES ('mails', 'mails', 'mail', 'mails');
        INSERT INTO fields(entity, field, db_column, type, display_label)
        VALUES ('mails', 'subject', 'subject', 'text', 'subject');
        INSERT INTO sample_values(field_canonical, examples, pii_redacted)
        VALUES ('mails.subject', '["Welcome"]', 0);
        """
    )
    conn.close()


def _mail_subject_proposal() -> dict[str, object]:
    return {
        "schema_version": 1,
        "action": "route",
        "confidence": 0.9,
        "intent": "list mail subjects",
        "target_entities": ["mails"],
        "projections": [
            {
                "kind": "field",
                "field": "mails.subject",
                "aggregate": "",
                "alias": "",
                "rationale": "subject is the requested display text",
            }
        ],
        "filters": [],
        "joins": [],
        "group_by": [],
        "order_by": [],
        "limit": 100,
        "ambiguity_questions": [],
        "evidence": [
            {
                "claim": "mails.subject is in the packet",
                "graph_refs": ["mails.subject"],
            }
        ],
        "safety_notes": [],
    }


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
    assert data["schema_version"] == 2
    assert data["metadata"]["dataset_hash"]
    assert data["metadata"]["graph_cache_dir"] == str(tmp_path / "graphs")
    assert data["summary"]["failure_buckets"]["correct"] == 1
    assert data["summary"]["failure_buckets"]["needs_model"] == 1
    # Stage breakdown: one example pinned at Stage 0a, the other at
    # `needs_model` because it's a complex question Stage 1+ would
    # handle.
    assert data["summary"]["stage_breakdown"]["stage_0a"] == 1
    assert data["summary"]["stage_breakdown"]["needs_model"] == 1
    # Per-example records also carry the stage tag for drill-down.
    pinned_set = {r["stage_pinned"] for r in data["examples"]}
    assert pinned_set == {"stage_0a", "needs_model"}
    assert {r["exec_equal"] for r in data["examples"]} == {True, False}
    assert {r["failure_bucket"] for r in data["examples"]} == {"correct", "needs_model"}
    # Plain-text summary surfaces the per-stage breakdown line.
    assert "stages:" in proc.stdout


def test_oracle_cache_helpers_load_skeleton_and_schema(tmp_path: Path) -> None:
    from semsql_eval.__main__ import (
        _canonicalize_field_target,
        _load_oracle_cache,
        _oracle_schema_payload,
        _oracle_slots_payload,
    )

    cache = tmp_path / "teacher.jsonl"
    cache.write_text(
        json.dumps(
            {
                "db_id": "demo",
                "nl": "show tenant ids",
                "natsql_skeleton": "SELECT @field1 FROM @entity1",
                "ranked_schema": [
                    {"kind": "entity", "score": 1.0, "target": "tenants"},
                    {"kind": "field", "score": 0.9, "target": "tenants.id"},
                    {"kind": "fk", "score": 0.8, "target": "users.tenant_id = tenants.id"},
                ],
                "slot_map": {
                    "@entity1": "tenants",
                    "@field1": "tenants.id",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    records = _load_oracle_cache(cache)
    record = records[("demo", "show tenant ids")]
    assert record["natsql_skeleton"] == "SELECT @field1 FROM @entity1"
    assert record["slot_map"] == {"@entity1": "tenants", "@field1": "tenants.id"}
    assert _oracle_schema_payload(record) == {
        "entities": ["tenants"],
        "fields": ["tenants.id"],
        "top_score": 1.0,
    }
    assert _oracle_slots_payload(record) == {
        "@entity1": "tenants",
        "@field1": "tenants.id",
    }
    assert _canonicalize_field_target("frpm.Charter School (Y/N)") == (
        "frpm.charter_school_y_n"
    )


def test_gate_report_fails_bird_smoke_below_full_gate(tmp_path: Path) -> None:
    report = tmp_path / "bird100.json"
    report.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "summary": {
                    "suite": "bird",
                    "total": 100,
                    "correct": 1,
                    "wrong": 99,
                    "bailed": 0,
                    "errored": 0,
                    "timeouts": 0,
                    "exec_acc": 0.01,
                    "failure_buckets": {"exec_mismatch": 99, "correct": 1},
                },
                "examples": [],
            }
        ),
        encoding="utf-8",
    )
    out = tmp_path / "gate.md"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "semsql_eval",
            "gate-report",
            "--profile",
            "v0.2-bird",
            "--report-json",
            str(report),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_subprocess_env(),
    )
    assert proc.returncode == 1
    assert "expected full BIRD dev total" in proc.stdout
    assert "expected exec_acc >= 35.0%" in proc.stdout
    assert out.exists()


def test_gate_report_passes_full_bird_threshold(tmp_path: Path) -> None:
    report = tmp_path / "bird-full.json"
    report.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "summary": {
                    "suite": "bird",
                    "total": 1534,
                    "correct": 537,
                    "wrong": 997,
                    "bailed": 0,
                    "errored": 0,
                    "timeouts": 0,
                    "exec_acc": 0.3500651890482399,
                    "failure_buckets": {"exec_mismatch": 997, "correct": 537},
                },
                "examples": [],
            }
        ),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "semsql_eval",
            "gate-report",
            "--profile",
            "v0.2-bird",
            "--report-json",
            str(report),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_subprocess_env(),
    )
    assert proc.returncode == 0, proc.stderr
    assert "PASSED" in proc.stdout


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


def test_build_queryframe_canary_cli_writes_layout(tmp_path: Path) -> None:
    out = tmp_path / "queryframe_canary"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "semsql_eval",
            "build-queryframe-canary",
            "--out",
            str(out),
            "--seed",
            "42",
            "--variant",
            "alias",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_subprocess_env(),
    )
    assert proc.returncode == 0, proc.stderr
    assert "queryframe canary written" in proc.stdout
    assert (out / "dev.json").exists()
    assert (out / "tables.json").exists()
    assert (out / "queryframe_canary.json").exists()
    metadata = json.loads((out / "queryframe_canary.json").read_text())
    assert metadata["variant"] == "alias"
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


def test_build_platform_query_suite_cli_writes_layout(tmp_path: Path) -> None:
    out = tmp_path / "platform_suite"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "semsql_eval",
            "build-platform-query-suite",
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_subprocess_env(),
    )
    assert proc.returncode == 0, proc.stderr
    assert "platform query suite written" in proc.stdout
    assert (out / "platform_query_suite.json").exists()
    assert (out / "questions.jsonl").exists()
    assert (out / "database" / "growth_ops" / "growth_ops.sqlite").exists()


def test_build_platform_query_suite_cli_help_lists_exports() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "semsql_eval",
            "build-platform-query-suite",
            "--help",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_subprocess_env(),
    )
    assert proc.returncode == 0
    assert "--out-json" in proc.stdout
    assert "--out-md" in proc.stdout


def test_build_business_analytics_suite_cli_writes_layout(tmp_path: Path) -> None:
    out = tmp_path / "business_suite"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "semsql_eval",
            "build-business-analytics-suite",
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_subprocess_env(),
    )
    assert proc.returncode == 0, proc.stderr
    assert "business analytics suite written" in proc.stdout
    assert (out / "platform_query_suite.json").exists()
    assert (out / "questions.jsonl").exists()
    assert (
        out
        / "database"
        / "business_analytics"
        / "business_analytics.sqlite"
    ).exists()


def test_semantic_atlas_assessment_cli_writes_report(tmp_path: Path) -> None:
    out = tmp_path / "semantic_atlas"
    report = tmp_path / "semantic_atlas.json"
    md = tmp_path / "semantic_atlas.md"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "semsql_eval",
            "semantic-atlas-assessment",
            "--suite",
            "platform",
            "--out",
            str(out),
            "--out-json",
            str(report),
            "--out-md",
            str(md),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_subprocess_env(),
    )
    assert proc.returncode == 0, proc.stderr
    assert report.exists()
    assert md.exists()
    assert "Mini SemanticAtlas Practical Assessment" in proc.stdout
    data = json.loads(report.read_text(encoding="utf-8"))
    assert data["summary"]["route_total"] == 11


def test_queryframe_canary_cli_help_lists_strict_mode() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "semsql_eval",
            "queryframe-canary",
            "--help",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_subprocess_env(),
    )
    assert proc.returncode == 0
    assert "--strict" in proc.stdout
    assert "--out-json" in proc.stdout
    assert "--variant" in proc.stdout


def test_pathway_benchmark_cli_help_lists_strict_mode() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "semsql_eval",
            "pathway-benchmark",
            "--help",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_subprocess_env(),
    )
    assert proc.returncode == 0
    assert "--strict" in proc.stdout
    assert "--schema-variant" in proc.stdout


def test_queryframe_canary_suite_cli_help_lists_matrix_options() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "semsql_eval",
            "queryframe-canary-suite",
            "--help",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_subprocess_env(),
    )
    assert proc.returncode == 0
    assert "--seed" in proc.stdout
    assert "--variant" in proc.stdout
    assert "--strict" in proc.stdout


def test_queryframe_canary_postgres_cli_help_lists_live_db_options() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "semsql_eval",
            "queryframe-canary-postgres",
            "--help",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_subprocess_env(),
    )
    assert proc.returncode == 0
    assert "--db-url" in proc.stdout
    assert "--keep-schema" in proc.stdout
    assert "--strict" in proc.stdout


def test_queryframe_canary_mysql_cli_help_lists_live_db_options() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "semsql_eval",
            "queryframe-canary-mysql",
            "--help",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_subprocess_env(),
    )
    assert proc.returncode == 0
    assert "--db-url" in proc.stdout
    assert "--keep-database" in proc.stdout
    assert "--strict" in proc.stdout


def test_realdb_schema_probe_mysql_cli_help_lists_safety_options() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "semsql_eval",
            "realdb-schema-probe-mysql",
            "--help",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_subprocess_env(),
    )
    assert proc.returncode == 0
    assert "--db-url" in proc.stdout
    assert "--database" in proc.stdout
    assert "--unsafe-prompt-count" in proc.stdout
    assert "--strict" in proc.stdout


def test_realdb_schema_probe_mysql_suite_cli_help_lists_safety_options() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "semsql_eval",
            "realdb-schema-probe-mysql-suite",
            "--help",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_subprocess_env(),
    )
    assert proc.returncode == 0
    assert "--db-url" in proc.stdout
    assert "--database" in proc.stdout
    assert "--seed" in proc.stdout
    assert "--unsafe-prompt-count" in proc.stdout
    assert "--strict" in proc.stdout


def test_realdb_schema_probe_postgres_cli_help_lists_safety_options() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "semsql_eval",
            "realdb-schema-probe-postgres",
            "--help",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_subprocess_env(),
    )
    assert proc.returncode == 0
    assert "--db-url" in proc.stdout
    assert "--database" in proc.stdout
    assert "--unsafe-prompt-count" in proc.stdout
    assert "--strict" in proc.stdout


def test_realdb_schema_probe_postgres_suite_cli_help_lists_safety_options() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "semsql_eval",
            "realdb-schema-probe-postgres-suite",
            "--help",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_subprocess_env(),
    )
    assert proc.returncode == 0
    assert "--db-url" in proc.stdout
    assert "--database" in proc.stdout
    assert "--seed" in proc.stdout
    assert "--unsafe-prompt-count" in proc.stdout
    assert "--strict" in proc.stdout


def test_realdb_typed_fallback_mysql_cli_help_lists_provider_options() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "semsql_eval",
            "realdb-typed-fallback-mysql",
            "--help",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_subprocess_env(),
    )
    assert proc.returncode == 0
    assert "--db-url" in proc.stdout
    assert "--probe-count" in proc.stdout
    assert "--family" in proc.stdout
    assert "filtered_grouped_avg" in proc.stdout
    assert "multi_series_grouped_avg" in proc.stdout
    assert "value_filtered_grouped_avg" in proc.stdout
    assert "joined_filtered_grouped_avg" in proc.stdout
    assert "multi_joined_filtered_grouped_avg" in proc.stdout
    assert "--provider" in proc.stdout
    assert "--include-sample-values" in proc.stdout
    assert "--strict" in proc.stdout


def test_realdb_typed_fallback_postgres_cli_help_lists_provider_options() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "semsql_eval",
            "realdb-typed-fallback-postgres",
            "--help",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_subprocess_env(),
    )
    assert proc.returncode == 0
    assert "--db-url" in proc.stdout
    assert "--probe-count" in proc.stdout
    assert "--family" in proc.stdout
    assert "filtered_grouped_avg" in proc.stdout
    assert "multi_series_grouped_avg" in proc.stdout
    assert "value_filtered_grouped_avg" in proc.stdout
    assert "joined_filtered_grouped_avg" in proc.stdout
    assert "multi_joined_filtered_grouped_avg" in proc.stdout
    assert "--provider" in proc.stdout
    assert "--include-sample-values" in proc.stdout
    assert "--strict" in proc.stdout


def test_realdb_typed_fallback_postgres_suite_cli_help_lists_provider_options() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "semsql_eval",
            "realdb-typed-fallback-postgres-suite",
            "--help",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_subprocess_env(),
    )
    assert proc.returncode == 0
    assert "--db-url" in proc.stdout
    assert "--seed" in proc.stdout
    assert "--probe-count" in proc.stdout
    assert "--family" in proc.stdout
    assert "multi_series_grouped_avg" in proc.stdout
    assert "--provider" in proc.stdout
    assert "--include-sample-values" in proc.stdout
    assert "--strict" in proc.stdout


def test_realdb_typed_fallback_postgres_missing_url_skips(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from semsql_eval.__main__ import _run_realdb_typed_fallback_mysql

    monkeypatch.delenv("SEMSQL_POSTGRES_PROBE_URL", raising=False)

    report = _run_realdb_typed_fallback_mysql(
        out_dir=tmp_path / "typed-postgres",
        seed=20260604,
        db_url=None,
        database=None,
        probe_count=1,
        families=("rate",),
        provider="none",
        provider_base_url=None,
        provider_api_key_env=None,
        model=None,
        semsql_bin=tmp_path / "missing-semsql",
        graph_cache_dir=None,
        timeout_seconds=1,
        extract_timeout_seconds=1,
        exec_timeout_seconds=1.0,
        include_sample_values=False,
        include_generated=False,
        engine="postgres",
    )

    assert report["engine"] == "postgres"
    assert report["status"] == "skipped"
    assert report["skip_reason"] == "missing_db_url"
    assert "SEMSQL_POSTGRES_PROBE_URL" in report["skip_detail"]


def test_grouped_avg_sql_match_requires_group_field_in_group_by() -> None:
    sql = (
        "SELECT `fraud_reports`.`status`, AVG(`fraud_reports`.`amount`) AS `avg_amount` "
        "FROM `fraud_reports` "
        "WHERE `fraud_reports`.`has_police_report` = 1 "
        "GROUP BY `fraud_reports`.`status` "
        "ORDER BY `avg_amount` DESC LIMIT 1"
    )

    assert not _grouped_avg_sql_mentions_expected_fields(
        sql,
        table="fraud_reports",
        metric_field="amount",
        group_field="has_police_report",
    )
    assert _grouped_avg_sql_mentions_expected_fields(
        sql,
        table="fraud_reports",
        metric_field="amount",
        group_field="status",
    )


def test_typed_fallback_value_filtered_grouped_sql_match_requires_sample_filter() -> None:
    sql = (
        "SELECT `campaign_events`.`status`, "
        "AVG(`campaign_events`.`score`) AS `avg_score` "
        "FROM `campaign_events` "
        "WHERE `campaign_events`.`channel` = 'paid_search' "
        "GROUP BY `campaign_events`.`status` "
        "ORDER BY `avg_score` DESC LIMIT 1"
    )

    assert _selected_sql_matches_typed_fallback_question(
        sql,
        question={
            "expected_kind": "value_filtered_grouped_avg",
            "expected_table": "campaign_events",
            "expected_metric_field": "score",
            "expected_group_field": "status",
            "expected_filter_field": "channel",
            "expected_filter_value": "paid_search",
        },
    )
    assert not _selected_sql_matches_typed_fallback_question(
        sql.replace("paid_search", "organic"),
        question={
            "expected_kind": "value_filtered_grouped_avg",
            "expected_table": "campaign_events",
            "expected_metric_field": "score",
            "expected_group_field": "status",
            "expected_filter_field": "channel",
            "expected_filter_value": "paid_search",
        },
    )


def test_typed_fallback_multi_series_sql_match_requires_time_and_group() -> None:
    sql = (
        "SELECT DATE(`campaign_events`.`created_at`) AS `day`, "
        "`campaign_events`.`channel`, "
        "AVG(`campaign_events`.`score`) AS `avg_score` "
        "FROM `campaign_events` "
        "GROUP BY DATE(`campaign_events`.`created_at`), `campaign_events`.`channel` "
        "ORDER BY `day` ASC"
    )
    question = {
        "expected_kind": "multi_series_grouped_avg",
        "expected_table": "campaign_events",
        "expected_metric_field": "score",
        "expected_time_field": "created_at",
        "expected_group_field": "channel",
    }

    assert _selected_sql_matches_typed_fallback_question(sql, question=question)
    assert not _selected_sql_matches_typed_fallback_question(
        sql.replace("DATE(`campaign_events`.`created_at`) AS `day`, ", ""),
        question=question,
    )


def test_realdb_typed_fallback_mysql_suite_cli_help_lists_provider_options() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "semsql_eval",
            "realdb-typed-fallback-mysql-suite",
            "--help",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_subprocess_env(),
    )
    assert proc.returncode == 0
    assert "--db-url" in proc.stdout
    assert "--seed" in proc.stdout
    assert "--probe-count" in proc.stdout
    assert "--family" in proc.stdout
    assert "value_filtered_grouped_avg" in proc.stdout
    assert "multi_series_grouped_avg" in proc.stdout
    assert "multi_joined_filtered_grouped_avg" in proc.stdout
    assert "--provider" in proc.stdout
    assert "--include-sample-values" in proc.stdout
    assert "--strict" in proc.stdout


def test_realdb_typed_fallback_packet_schema_evidence_audits_provider_request(
    tmp_path: Path,
) -> None:
    full_packet = {
        "schema_card": {
            "entities": [
                {
                    "name": "campaigns",
                    "fields": [
                        {"name": "status"},
                        {"name": "total_recipients"},
                    ],
                }
            ]
        }
    }
    compact_provider_packet = {
        "schema_card": {
            "entities": [
                {
                    "name": "campaigns",
                    "fields": [{"name": "status"}],
                }
            ]
        }
    }
    packet_path = tmp_path / "rejected.packet.json"
    request_path = tmp_path / "openai-request.json"
    packet_path.write_text(json.dumps(full_packet), encoding="utf-8")
    request_path.write_text(
        json.dumps({"input": json.dumps(compact_provider_packet)}),
        encoding="utf-8",
    )

    evidence = _realdb_typed_fallback_packet_schema_evidence(
        question={
            "expected_kind": "grouped_avg",
            "expected_table": "campaigns",
            "expected_metric_field": "total_recipients",
            "expected_group_field": "status",
        },
        artifacts={
            "packet": str(packet_path),
            "openai_request": str(request_path),
        },
    )

    assert evidence["full_packet"]["checked"] is True
    assert evidence["full_packet"]["missing"] == []
    assert evidence["provider_request"]["checked"] is True
    assert evidence["provider_request"]["missing"] == [
        "field:campaigns.total_recipients"
    ]


def test_realdb_typed_fallback_packet_schema_evidence_summary_counts() -> None:
    summary = _summarize_realdb_packet_schema_evidence(
        [
            {
                "packet_schema_evidence": {
                    "full_packet": {
                        "checked": True,
                        "missing_count": 0,
                    },
                    "provider_request": {
                        "checked": True,
                        "missing_count": 2,
                    },
                }
            },
            {
                "packet_schema_evidence": {
                    "full_packet": {
                        "checked": False,
                        "missing_count": 0,
                    },
                    "provider_request": {
                        "checked": True,
                        "missing_count": 0,
                    },
                }
            },
        ]
    )

    assert summary["full_packet"] == {
        "checked_records": 1,
        "missing_records": 0,
        "missing_total": 0,
    }
    assert summary["provider_request"] == {
        "checked_records": 2,
        "missing_records": 1,
        "missing_total": 2,
    }


def test_realdb_typed_fallback_provider_readiness_summary_counts() -> None:
    summary = _summarize_realdb_typed_fallback_records(
        [
            {
                "provider_readiness": {
                    "provider": "openai",
                    "configured": False,
                    "missing_env": ["OPENAI_API_KEY"],
                }
            },
            {
                "provider_readiness": {
                    "provider": "openai",
                    "configured": True,
                    "missing_env": [],
                }
            },
        ],
        provider="openai",
        sample_value_rows=0,
        sample_values_allowed=False,
    )

    assert summary["provider_readiness"] == {
        "checked_records": 2,
        "configured_records": 1,
        "unconfigured_records": 1,
        "missing_env_counts": {"OPENAI_API_KEY": 1},
        "provider_counts": {"openai": 2},
    }


def test_realdb_typed_fallback_summary_requires_result_shape() -> None:
    base_record = {
        "selected_sql": "select avg(campaign_events.score) as avg_score from campaign_events",
        "selected_source": "typed_fallback",
        "provider_call_count": 1,
        "execution_status": "ok",
        "expected_match": True,
        "ok": True,
        "provider_error": None,
        "fallback_render_valid": True,
        "rows_retained": False,
        "packet_schema_evidence": {
            "full_packet": {"checked": True, "missing_count": 0},
            "provider_request": {"checked": True, "missing_count": 0},
        },
    }
    missing_shape = _summarize_realdb_typed_fallback_records(
        [base_record],
        provider="openai",
        sample_value_rows=0,
        sample_values_allowed=False,
    )

    with_shape_record = {
        **base_record,
        "result_shape_kind": "scalar_metric",
        "result_shape_ok": True,
        "result_shape": {"kind": "scalar_metric"},
    }
    with_shape = _summarize_realdb_typed_fallback_records(
        [with_shape_record],
        provider="openai",
        sample_value_rows=0,
        sample_values_allowed=False,
    )

    assert missing_shape["result_shape_ok"] == 0
    assert missing_shape["result_shape_counts"] == {"missing": 1}
    assert missing_shape["pass"] is False
    assert with_shape["result_shape_ok"] == 1
    assert with_shape["result_shape_counts"] == {"scalar_metric": 1}
    assert with_shape["pass"] is True


def test_realdb_typed_fallback_summary_fails_on_packet_schema_loss() -> None:
    summary = _summarize_realdb_typed_fallback_records(
        [
            {
                "selected_sql": "select campaigns.status from campaigns",
                "selected_source": "typed_fallback",
                "provider_call_count": 1,
                "execution_status": "ok",
                "expected_match": True,
                "ok": True,
                "provider_error": None,
                "fallback_render_valid": True,
                "rows_retained": False,
                "packet_schema_evidence": {
                    "full_packet": {
                        "checked": True,
                        "missing_count": 0,
                    },
                    "provider_request": {
                        "checked": True,
                        "missing_count": 1,
                    },
                },
            }
        ],
        provider="openai",
        sample_value_rows=0,
        sample_values_allowed=False,
    )

    assert summary["packet_schema_evidence_ok"] is False
    assert summary["pass"] is False


def test_realdb_shard_audit_mysql_cli_help_lists_safety_options() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "semsql_eval",
            "realdb-shard-audit-mysql",
            "--help",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_subprocess_env(),
    )
    assert proc.returncode == 0
    assert "--db-url" in proc.stdout
    assert "--database" in proc.stdout
    assert "--source-root" in proc.stdout
    assert "--strict" in proc.stdout


def test_schema_card_cli_help_lists_graph_option() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "semsql_eval",
            "schema-card",
            "--help",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_subprocess_env(),
    )
    assert proc.returncode == 0
    assert "--graph" in proc.stdout
    assert "--include-samples" in proc.stdout


def test_llm_resolution_packet_cli_help_lists_openai_opt_in() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "semsql_eval",
            "llm-resolution-packet",
            "--help",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_subprocess_env(),
    )
    assert proc.returncode == 0
    assert "--route-reason" in proc.stdout
    assert "--openai" in proc.stdout


def test_llm_resolution_validate_cli_help_lists_strict_option() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "semsql_eval",
            "llm-resolution-validate",
            "--help",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_subprocess_env(),
    )
    assert proc.returncode == 0
    assert "--packet-json" in proc.stdout
    assert "--proposal-json" in proc.stdout
    assert "--strict" in proc.stdout


def test_llm_resolution_render_cli_help_lists_dialect_option() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "semsql_eval",
            "llm-resolution-render",
            "--help",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_subprocess_env(),
    )
    assert proc.returncode == 0
    assert "--packet-json" in proc.stdout
    assert "--proposal-json" in proc.stdout
    assert "--dialect" in proc.stdout
    assert "--strict" in proc.stdout


def test_llm_resolution_render_batch_cli_help_lists_strict_option() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "semsql_eval",
            "llm-resolution-render-batch",
            "--help",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_subprocess_env(),
    )
    assert proc.returncode == 0
    assert "--packet-dir" in proc.stdout
    assert "--proposal-dir" in proc.stdout
    assert "--strict" in proc.stdout


def test_llm_resolution_resolve_batch_cli_help_lists_provider_option() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "semsql_eval",
            "llm-resolution-resolve-batch",
            "--help",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_subprocess_env(),
    )
    assert proc.returncode == 0
    assert "--packet-dir" in proc.stdout
    assert "--provider" in proc.stdout
    assert "--overwrite" in proc.stdout
    assert "--strict" in proc.stdout


def test_llm_resolution_resolve_packet_cli_help_lists_product_options() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "semsql_eval",
            "llm-resolution-resolve-packet",
            "--help",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_subprocess_env(),
    )
    assert proc.returncode == 0
    assert "--packet-json" in proc.stdout
    assert "--proposal-json" in proc.stdout
    assert "--provider" in proc.stdout
    assert "--render-out" in proc.stdout
    assert "--execute-sqlite" in proc.stdout
    assert "--execution-out" in proc.stdout
    assert "--discard-execution-rows" in proc.stdout
    assert "--strict" in proc.stdout


def test_llm_resolution_openai_request_batch_cli_help_lists_output_option() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "semsql_eval",
            "llm-resolution-openai-request-batch",
            "--help",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_subprocess_env(),
    )
    assert proc.returncode == 0
    assert "--packet-dir" in proc.stdout
    assert "--out" in proc.stdout
    assert "--model" in proc.stdout
    assert "--max-cases" in proc.stdout


def test_llm_resolution_capture_query_cli_help_lists_capture_options() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "semsql_eval",
            "llm-resolution-capture-query",
            "--help",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_subprocess_env(),
    )
    assert proc.returncode == 0
    assert "--graph" in proc.stdout
    assert "--question" in proc.stdout
    assert "--semsql-bin" in proc.stdout
    assert "--include-samples" in proc.stdout
    assert "--proposal-json" in proc.stdout
    assert "--strict-render" in proc.stdout


def test_llm_resolution_capture_query_writes_packet_for_local_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from semsql_eval.__main__ import cli

    graph = tmp_path / "g.semsql"
    _make_semantic_graph(graph)
    out_dir = tmp_path / "capture"
    proposal_json = tmp_path / "proposal.json"
    proposal_json.write_text(json.dumps(_mail_subject_proposal()), encoding="utf-8")

    def fake_run_cascade_query(*_args: object, **kwargs: object) -> SimpleNamespace:
        frame_path = kwargs["query_frame_json"]
        assert isinstance(frame_path, Path)
        frame_path.write_text('{"intent":"row_lookup"}\n', encoding="utf-8")
        return SimpleNamespace(
            sql=None,
            stage_pinned="frame_rejected",
            error_detail="route rejected",
            elapsed_seconds=0.01,
            stdout_bytes=0,
            stderr_bytes=12,
            query_frame={"intent": "row_lookup"},
        )

    monkeypatch.setattr(
        "semsql_eval.__main__.run_cascade_query",
        fake_run_cascade_query,
    )

    result = CliRunner().invoke(
        cli,
        [
            "llm-resolution-capture-query",
            "--graph",
            str(graph),
            "--question",
            "show mail subjects",
            "--out",
            str(out_dir),
            "--include-samples",
            "--proposal-json",
            str(proposal_json),
            "--strict-render",
        ],
    )

    assert result.exit_code == 0, result.output
    summary = json.loads((out_dir / "capture.json").read_text(encoding="utf-8"))
    request = json.loads((out_dir / "openai-request.json").read_text(encoding="utf-8"))
    packet = json.loads((out_dir / "rejected.packet.json").read_text(encoding="utf-8"))
    render = json.loads((out_dir / "render.json").read_text(encoding="utf-8"))
    assert summary["routed_locally"] is False
    assert summary["provider_call_count"] == 0
    assert summary["openai_request_strict"] is True
    assert summary["fallback_render_valid"] is True
    assert summary["render_written"] is True
    assert summary["artifacts"]["packet"] == str(out_dir / "rejected.packet.json")
    assert summary["artifacts"]["render"] == str(out_dir / "render.json")
    assert request["text"]["format"]["strict"] is True
    assert packet["route_reason"] == "frame_rejected"
    assert packet["query_frame"] == {"intent": "row_lookup"}
    assert packet["schema_card"]["summary"]["sample_values_included"] is True
    assert render["valid"] is True
    assert render["issues"] == []
    assert 'SELECT "mails"."subject" FROM "mails"' in render["sql"]


def test_llm_resolution_fallback_query_cli_help_lists_safe_options() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "semsql_eval",
            "llm-resolution-fallback-query",
            "--help",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_subprocess_env(),
    )
    assert proc.returncode == 0
    assert "--provider" in proc.stdout
    assert "--proposal-json" in proc.stdout
    assert "--strict" in proc.stdout
    assert "--include-samples" in proc.stdout
    assert "--execute-db-url" in proc.stdout
    assert "--execute-sqlite" in proc.stdout
    assert "--discard-execution-rows" in proc.stdout
    assert "--clarification-choice" in proc.stdout


def test_llm_resolution_fallback_batch_cli_help_lists_batch_options() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "semsql_eval",
            "llm-resolution-fallback-batch",
            "--help",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_subprocess_env(),
    )
    assert proc.returncode == 0
    assert "--packet-dir" in proc.stdout
    assert "--proposal-dir" in proc.stdout
    assert "--provider" in proc.stdout
    assert "--max-cases" in proc.stdout
    assert "--clarification-choices-json" in proc.stdout
    assert "--execute-db-url" in proc.stdout
    assert "--execute-db-url-json" in proc.stdout
    assert "--execute-sqlite" in proc.stdout
    assert "--discard-execution-rows" in proc.stdout
    assert "--strict" in proc.stdout


def test_llm_resolution_fallback_query_selects_local_sql_without_packet(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from semsql_eval.__main__ import cli

    graph = tmp_path / "g.semsql"
    _make_semantic_graph(graph)
    db_path = tmp_path / "mail.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE mails (subject TEXT);
        INSERT INTO mails VALUES ('Welcome'), ('Renewal');
        """
    )
    conn.close()
    out_dir = tmp_path / "fallback-local"

    def fake_run_cascade_query(*_args: object, **kwargs: object) -> SimpleNamespace:
        frame_path = kwargs["query_frame_json"]
        assert isinstance(frame_path, Path)
        frame_path.write_text('{"intent":"local"}\n', encoding="utf-8")
        return SimpleNamespace(
            sql='SELECT "mails"."subject" FROM "mails"',
            stage_pinned="stage_0a",
            error_detail=None,
            elapsed_seconds=0.01,
            query_frame={"intent": "local"},
        )

    monkeypatch.setattr(
        "semsql_eval.__main__.run_cascade_query",
        fake_run_cascade_query,
    )

    result = CliRunner().invoke(
        cli,
        [
            "llm-resolution-fallback-query",
            "--graph",
            str(graph),
            "--question",
            "show mail subjects",
            "--out",
            str(out_dir),
            "--execute-sqlite",
            str(db_path),
            "--max-rows",
            "1",
            "--strict",
        ],
    )

    assert result.exit_code == 0, result.output
    summary = json.loads((out_dir / "fallback-query.json").read_text(encoding="utf-8"))
    assert summary["status"] == "selected"
    assert summary["selected_source"] == "local"
    assert summary["selected_sql"] == 'SELECT "mails"."subject" FROM "mails"'
    assert summary["provider_call_count"] == 0
    assert summary["used_direct_llm_sql"] is False
    assert summary["result_shape"]["kind"] == "table"
    assert summary["execution"]["status"] == "ok"
    assert summary["execution"]["rows"] == [["Welcome"]]
    assert summary["execution"]["truncated"] is True
    assert summary["artifacts"]["execution"] == str(out_dir / "execution.json")
    assert summary["artifacts"]["packet"] is None
    assert not (out_dir / "rejected.packet.json").exists()
    assert "## Execution" in result.output


def test_llm_resolution_fallback_query_demotes_local_shape_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from semsql_eval.__main__ import cli

    graph = tmp_path / "g.semsql"
    _make_semantic_graph(graph)
    conn = sqlite3.connect(graph)
    conn.executemany(
        "INSERT INTO fields(entity, field, db_column, type, display_label) "
        "VALUES ('mails', ?, ?, ?, ?)",
        [
            ("status", "status", "text", "status"),
            ("score", "score", "integer", "score"),
            ("ordered_on", "ordered_on", "date", "ordered on"),
        ],
    )
    conn.commit()
    conn.close()
    out_dir = tmp_path / "fallback-shape-mismatch"

    def fake_run_cascade_query(*_args: object, **kwargs: object) -> SimpleNamespace:
        frame_path = kwargs["query_frame_json"]
        assert isinstance(frame_path, Path)
        frame_path.write_text('{"intent":"wrong_shape"}\n', encoding="utf-8")
        return SimpleNamespace(
            sql=(
                'SELECT "mails"."status", AVG("mails"."score") AS "avg_score" '
                'FROM "mails" GROUP BY "mails"."status"'
            ),
            stage_pinned="stage_0a",
            error_detail=None,
            elapsed_seconds=0.01,
            query_frame={"intent": "wrong_shape"},
        )

    monkeypatch.setattr(
        "semsql_eval.__main__.run_cascade_query",
        fake_run_cascade_query,
    )

    result = CliRunner().invoke(
        cli,
        [
            "llm-resolution-fallback-query",
            "--graph",
            str(graph),
            "--question",
            "show average score by status over ordered on for mails",
            "--out",
            str(out_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    summary = json.loads((out_dir / "fallback-query.json").read_text(encoding="utf-8"))
    packet = json.loads((out_dir / "rejected.packet.json").read_text(encoding="utf-8"))
    assert summary["status"] == "unresolved"
    assert summary["selected_source"] is None
    assert summary["selected_sql"] is None
    assert summary["local_routed"] is True
    assert summary["local_sql_rejected_reason"] == (
        "local_route_shape_mismatch:requested_multi_series_time_dimension"
    )
    assert summary["local_result_shape"]["kind"] == "categorical_chart"
    assert packet["route_reason"] == summary["local_sql_rejected_reason"]
    assert summary["artifacts"]["packet"] == str(out_dir / "rejected.packet.json")


def test_llm_resolution_fallback_query_selects_validated_typed_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from semsql_eval.__main__ import cli

    graph = tmp_path / "g.semsql"
    _make_semantic_graph(graph)
    out_dir = tmp_path / "fallback"
    proposal_json = tmp_path / "proposal.json"
    proposal_json.write_text(json.dumps(_mail_subject_proposal()), encoding="utf-8")

    def fake_run_cascade_query(*_args: object, **kwargs: object) -> SimpleNamespace:
        frame_path = kwargs["query_frame_json"]
        assert isinstance(frame_path, Path)
        frame_path.write_text('{"intent":"needs_fallback"}\n', encoding="utf-8")
        return SimpleNamespace(
            sql=None,
            stage_pinned="needs_model",
            error_detail="needs model",
            elapsed_seconds=0.01,
            query_frame={"intent": "needs_fallback"},
        )

    monkeypatch.setattr(
        "semsql_eval.__main__.run_cascade_query",
        fake_run_cascade_query,
    )

    result = CliRunner().invoke(
        cli,
        [
            "llm-resolution-fallback-query",
            "--graph",
            str(graph),
            "--question",
            "show mail subjects",
            "--out",
            str(out_dir),
            "--proposal-json",
            str(proposal_json),
            "--strict",
            "--include-samples",
        ],
    )

    assert result.exit_code == 0, result.output
    summary = json.loads((out_dir / "fallback-query.json").read_text(encoding="utf-8"))
    render = json.loads((out_dir / "render.json").read_text(encoding="utf-8"))
    assert summary["status"] == "selected"
    assert summary["selected_source"] == "typed_fallback"
    assert summary["provider_call_count"] == 0
    assert summary["used_direct_llm_sql"] is False
    assert summary["fallback_render_valid"] is True
    assert summary["artifacts"]["packet"] == str(out_dir / "rejected.packet.json")
    assert summary["artifacts"]["render"] == str(out_dir / "render.json")
    assert render["valid"] is True
    assert render["issues"] == []
    assert 'SELECT "mails"."subject" FROM "mails"' in summary["selected_sql"]


def test_llm_resolution_fallback_query_redacts_skipped_execution_db_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from semsql_eval.__main__ import cli

    graph = tmp_path / "g.semsql"
    _make_semantic_graph(graph)
    out_dir = tmp_path / "fallback-unresolved"

    def fake_run_cascade_query(*_args: object, **kwargs: object) -> SimpleNamespace:
        frame_path = kwargs["query_frame_json"]
        assert isinstance(frame_path, Path)
        frame_path.write_text('{"intent":"needs_fallback"}\n', encoding="utf-8")
        return SimpleNamespace(
            sql=None,
            stage_pinned="needs_model",
            error_detail="needs model",
            elapsed_seconds=0.01,
            query_frame={"intent": "needs_fallback"},
        )

    monkeypatch.setattr(
        "semsql_eval.__main__.run_cascade_query",
        fake_run_cascade_query,
    )

    result = CliRunner().invoke(
        cli,
        [
            "llm-resolution-fallback-query",
            "--graph",
            str(graph),
            "--question",
            "show mail subjects",
            "--out",
            str(out_dir),
            "--execute-db-url",
            "mariadb://root:password@localhost:3306/app_db",
        ],
    )

    assert result.exit_code == 0, result.output
    summary = json.loads((out_dir / "fallback-query.json").read_text(encoding="utf-8"))
    execution = json.loads((out_dir / "execution.json").read_text(encoding="utf-8"))
    assert summary["status"] == "unresolved"
    assert summary["execution"]["target"] == "mariadb://root:***@localhost:3306/app_db"
    assert execution["target"] == "mariadb://root:***@localhost:3306/app_db"
    assert "root:password" not in result.output
    assert "root:password" not in (out_dir / "fallback-query.md").read_text(encoding="utf-8")


def test_llm_resolution_fallback_query_summarizes_schema_path_clarification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from semsql_eval.__main__ import cli

    graph = tmp_path / "g.semsql"
    _make_semantic_graph(graph)
    conn = sqlite3.connect(graph)
    conn.executescript(
        """
        INSERT INTO entities(canonical_name, db_table, singular_label, plural_label)
        VALUES ('organizations', 'organizations', 'organization', 'organizations');
        INSERT INTO fields(entity, field, db_column, type, display_label)
        VALUES
          ('organizations', 'id', 'id', 'integer', 'ID'),
          ('organizations', 'name', 'name', 'text', 'Name'),
          ('mails', 'organization_id', 'organization_id', 'integer', 'Organization ID'),
          ('mails', 'owner_organization_id', 'owner_organization_id', 'integer', 'Owner Organization ID');
        INSERT INTO relationships(from_entity, from_field, to_entity, to_field, kind)
        VALUES
          ('mails', 'organization_id', 'organizations', 'id', 'many_to_one'),
          ('mails', 'owner_organization_id', 'organizations', 'id', 'many_to_one');
        """
    )
    conn.close()
    proposal = _mail_subject_proposal()
    proposal.update(
        {
            "action": "clarify",
            "result_shape": "categorical_chart",
            "target_entities": ["mails", "organizations"],
            "projections": [
                {
                    "kind": "count",
                    "field": "",
                    "aggregate": "COUNT",
                    "alias": "mail_count",
                    "rationale": "count mails",
                },
                {
                    "kind": "field",
                    "field": "organizations.name",
                    "aggregate": "",
                    "alias": "organization",
                    "rationale": "display organization",
                },
            ],
            "group_by": ["organizations.name"],
            "ambiguity_questions": [
                "Which organization path should be used: mails.organization_id or "
                "mails.owner_organization_id via mails.owner_organization_id \u2192 organizations.id?"
            ],
            "evidence": [
                {
                    "claim": "Two organization paths exist.",
                    "graph_refs": [
                        "relationships.mails.organization_id->organizations.id",
                        "relationships.mails.owner_organization_id->organizations.id",
                    ],
                }
            ],
        }
    )
    proposal_path = tmp_path / "proposal.json"
    proposal_path.write_text(json.dumps(proposal), encoding="utf-8")
    out_dir = tmp_path / "fallback-path-clarify"

    def fake_run_cascade_query(*_args: object, **kwargs: object) -> SimpleNamespace:
        frame_path = kwargs["query_frame_json"]
        assert isinstance(frame_path, Path)
        frame_path.write_text('{"intent":"needs_fallback"}\n', encoding="utf-8")
        return SimpleNamespace(
            sql=None,
            stage_pinned="needs_model",
            error_detail="needs model",
            elapsed_seconds=0.01,
            query_frame={"intent": "needs_fallback"},
        )

    monkeypatch.setattr(
        "semsql_eval.__main__.run_cascade_query",
        fake_run_cascade_query,
    )

    result = CliRunner().invoke(
        cli,
        [
            "llm-resolution-fallback-query",
            "--graph",
            str(graph),
            "--question",
            "count mails by organization",
            "--out",
            str(out_dir),
            "--proposal-json",
            str(proposal_path),
        ],
    )

    assert result.exit_code == 0, result.output
    summary = json.loads((out_dir / "fallback-query.json").read_text(encoding="utf-8"))
    assert summary["status"] == "unresolved"
    assert summary["fallback_render_issues"][0]["code"] == (
        "clarification_required_schema_path"
    )
    assert "mails.owner_organization_id" in summary["fallback_render_issues"][0][
        "candidate_fields"
    ]
    assert summary["fallback_render_issues"][0]["clarification_options"][1][
        "relationships"
    ] == [{"from": "mails.owner_organization_id", "to": "organizations.id"}]
    assert "->" in summary["fallback_render_issues"][0]["questions"][0]
    assert "\u2192" not in summary["fallback_render_issues"][0]["questions"][0]
    assert "clarification_required_schema_path" in result.output

    selected_out = tmp_path / "fallback-path-selected"
    selected = CliRunner().invoke(
        cli,
        [
            "llm-resolution-fallback-query",
            "--graph",
            str(graph),
            "--question",
            "count mails by organization",
            "--out",
            str(selected_out),
            "--proposal-json",
            str(proposal_path),
            "--clarification-choice",
            "schema_path_2",
        ],
    )

    assert selected.exit_code == 0, selected.output
    selected_summary = json.loads(
        (selected_out / "fallback-query.json").read_text(encoding="utf-8")
    )
    assert selected_summary["status"] == "selected"
    assert selected_summary["selected_source"] == "typed_fallback"
    assert selected_summary["clarification_choice"] == "schema_path_2"
    assert (
        '"mails"."owner_organization_id" = "organizations"."id"'
        in selected_summary["selected_sql"]
    )
    assert selected_summary["fallback_render_issues"][0]["code"] == (
        "clarification_choice_applied_schema_path"
    )


def test_llm_resolution_fallback_query_can_use_mocked_openai_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from semsql_eval.__main__ import cli

    graph = tmp_path / "g.semsql"
    _make_semantic_graph(graph)
    out_dir = tmp_path / "fallback-provider"

    def fake_run_cascade_query(*_args: object, **kwargs: object) -> SimpleNamespace:
        frame_path = kwargs["query_frame_json"]
        assert isinstance(frame_path, Path)
        frame_path.write_text('{"intent":"provider"}\n', encoding="utf-8")
        return SimpleNamespace(
            sql=None,
            stage_pinned="needs_model",
            error_detail="needs model",
            elapsed_seconds=0.01,
            query_frame={"intent": "provider"},
        )

    def fake_call_openai_resolution(
        packet: dict[str, object],
        *,
        model: str | None = None,
    ) -> dict[str, object]:
        assert packet["question"] == "show mail subjects"
        assert model == "gpt-test"
        return {
            "schema_version": 1,
            "source": "fake_openai",
            "model": model,
            "proposal": _mail_subject_proposal(),
        }

    monkeypatch.setattr(
        "semsql_eval.__main__.run_cascade_query",
        fake_run_cascade_query,
    )
    monkeypatch.setattr(
        "semsql_eval.__main__.call_openai_resolution",
        fake_call_openai_resolution,
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    result = CliRunner().invoke(
        cli,
        [
            "llm-resolution-fallback-query",
            "--graph",
            str(graph),
            "--question",
            "show mail subjects",
            "--out",
            str(out_dir),
            "--provider",
            "openai",
            "--model",
            "gpt-test",
            "--strict",
        ],
    )

    assert result.exit_code == 0, result.output
    summary = json.loads((out_dir / "fallback-query.json").read_text(encoding="utf-8"))
    provider_result = json.loads((out_dir / "openai.provider.json").read_text(encoding="utf-8"))
    assert summary["status"] == "selected"
    assert summary["selected_source"] == "typed_fallback"
    assert summary["provider_called"] is True
    assert summary["provider_call_count"] == 1
    assert summary["used_direct_llm_sql"] is False
    assert summary["fallback_render_valid"] is True
    assert summary["artifacts"]["provider_result"] == str(out_dir / "openai.provider.json")
    assert provider_result["source"] == "fake_openai"
    assert 'SELECT "mails"."subject" FROM "mails"' in summary["selected_sql"]


def test_llm_resolution_fallback_query_missing_provider_env_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from semsql_eval.__main__ import cli

    graph = tmp_path / "g.semsql"
    _make_semantic_graph(graph)
    out_dir = tmp_path / "fallback-provider-missing"

    def fake_run_cascade_query(*_args: object, **kwargs: object) -> SimpleNamespace:
        frame_path = kwargs["query_frame_json"]
        assert isinstance(frame_path, Path)
        frame_path.write_text('{"intent":"provider"}\n', encoding="utf-8")
        return SimpleNamespace(
            sql=None,
            stage_pinned="needs_model",
            error_detail="needs model",
            elapsed_seconds=0.01,
            query_frame={"intent": "provider"},
        )

    def provider_must_not_run(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("provider call should be skipped when env is missing")

    monkeypatch.setattr(
        "semsql_eval.__main__.run_cascade_query",
        fake_run_cascade_query,
    )
    monkeypatch.setattr(
        "semsql_eval.__main__.call_openai_resolution",
        provider_must_not_run,
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    result = CliRunner().invoke(
        cli,
        [
            "llm-resolution-fallback-query",
            "--graph",
            str(graph),
            "--question",
            "show mail subjects",
            "--out",
            str(out_dir),
            "--provider",
            "openai",
            "--model",
            "gpt-test",
        ],
    )

    assert result.exit_code == 0, result.output
    summary = json.loads((out_dir / "fallback-query.json").read_text(encoding="utf-8"))
    assert summary["status"] == "unresolved"
    assert summary["provider_called"] is False
    assert summary["provider_call_count"] == 0
    assert summary["provider_readiness"] == {
        "provider": "openai",
        "configured": False,
        "missing_env": ["OPENAI_API_KEY"],
        "skipped_reason": "provider_not_configured: OPENAI_API_KEY is not set",
    }
    assert summary["provider_error"] == "provider_not_configured: OPENAI_API_KEY is not set"
    assert summary["artifacts"]["packet"] == str(out_dir / "rejected.packet.json")
    assert summary["artifacts"]["openai_request"] == str(out_dir / "openai-request.json")
    assert summary["artifacts"]["provider_result"] is None
    assert "provider configured: `False`" in result.output


def test_typed_provider_readiness_does_not_expose_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "secret-test-key")

    readiness = _typed_provider_readiness("openai")

    assert readiness == {
        "provider": "openai",
        "configured": True,
        "missing_env": [],
        "skipped_reason": None,
    }
    assert "secret-test-key" not in json.dumps(readiness)


def test_typed_provider_readiness_supports_openai_compatible_presets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "secret-groq-key")
    monkeypatch.delenv("SEMSQL_OPENAI_COMPATIBLE_API_KEY", raising=False)
    monkeypatch.delenv("SEMSQL_OPENAI_COMPATIBLE_BASE_URL", raising=False)
    monkeypatch.delenv("SEMSQL_OPENAI_COMPATIBLE_MODEL", raising=False)

    groq = _typed_provider_readiness("groq")
    generic = _typed_provider_readiness("openai-compatible")

    assert groq["configured"] is True
    assert groq["missing_env"] == []
    assert groq["base_url"] == "https://api.groq.com/openai/v1"
    assert groq["model"] == "llama-3.3-70b-versatile"
    assert "secret-groq-key" not in json.dumps(groq)
    assert generic["configured"] is False
    assert generic["missing_env"] == ["SEMSQL_OPENAI_COMPATIBLE_API_KEY"]
    assert generic["missing_config"] == ["base_url", "model"]


def _rust_style_rejection_packet(graph: Path) -> dict[str, object]:
    return {
        "schema_version": 1,
        "source": "semsql_rejected_query_packet",
        "question": "show mail subjects",
        "route_reason": "needs_model",
        "schema_card": {
            "schema_version": 1,
            "source": "semsql_schema_card",
            "graph": str(graph),
            "summary": {
                "entity_count": 1,
                "field_count": 1,
                "relationship_count": 0,
                "sample_values_included": False,
            },
            "entities": [
                {
                    "name": "mails",
                    "db_table": "mails",
                    "labels": ["mail", "mails"],
                    "field_count": 1,
                    "fields": [
                        {
                            "name": "subject",
                            "db_column": "subject",
                            "type": "text",
                            "display_label": "subject",
                            "role": "display",
                            "samples": [],
                            "value_dictionary": [],
                        }
                    ],
                    "display_fields": ["subject"],
                    "id_fields": [],
                    "date_fields": [],
                    "status_fields": [],
                    "numeric_fields": [],
                    "sensitive": False,
                }
            ],
            "relationships": [],
            "safety": {
                "samples_policy": "omitted_by_default",
                "llm_may_not_execute_sql": True,
                "llm_sql_must_be_revalidated": True,
                "value_dictionary_policy": "field_scoped_scope_predicate_vocabulary_only",
            },
        },
        "local_candidates": {
            "entity_hits": [{"entity": "mails", "matched_tokens": ["mail"]}],
            "field_hits": [
                {
                    "field": "mails.subject",
                    "role": "display",
                    "matched_tokens": ["subject"],
                }
            ],
            "value_dictionary_hits": [],
            "ambiguous_physical_families_mentioned": [],
        },
        "query_frame": {
            "schema_version": 3,
            "source": "query_frame_error",
            "stage_pinned": "needs_model",
        },
        "allowed_resolution_contract": {
            "llm_output": "resolution_proposal_json",
            "must_not_emit_final_sql": True,
            "must_reference_schema_card_entities_and_fields": True,
            "value_filters_should_use_schema_card_value_dictionary": True,
            "must_ask_clarifying_questions_on_ambiguity": True,
            "semsql_must_validate_before_execution": True,
        },
    }


def _domains_rate_rejection_packet() -> dict[str, object]:
    return {
        "schema_version": 1,
        "source": "semsql_rejected_query_packet",
        "question": "what percentage of domains are SPF verified?",
        "route_reason": "needs_model",
        "schema_card": {
            "schema_version": 1,
            "source": "semsql_schema_card",
            "graph": "",
            "summary": {
                "entity_count": 1,
                "field_count": 2,
                "relationship_count": 0,
                "sample_values_included": True,
            },
            "entities": [
                {
                    "name": "domains",
                    "db_table": "domains",
                    "labels": ["domain", "domains"],
                    "field_count": 2,
                    "fields": [
                        {
                            "name": "id",
                            "db_column": "id",
                            "type": "integer",
                            "display_label": "id",
                            "role": "id",
                            "samples": [1, 2],
                            "value_dictionary": [],
                        },
                        {
                            "name": "is_spf_verified",
                            "db_column": "is_spf_verified",
                            "type": "boolean",
                            "display_label": "SPF verified",
                            "role": "boolean",
                            "samples": [True, False],
                            "value_dictionary": [
                                {
                                    "raw_value": True,
                                    "display": "SPF verified",
                                    "operator": "=",
                                }
                            ],
                        },
                    ],
                    "display_fields": [],
                    "id_fields": ["id"],
                    "date_fields": [],
                    "status_fields": ["is_spf_verified"],
                    "numeric_fields": [],
                    "sensitive": False,
                }
            ],
            "relationships": [],
            "safety": {
                "samples_policy": "non_pii_categorical_and_code_like_only",
                "llm_may_not_execute_sql": True,
                "llm_sql_must_be_revalidated": True,
                "value_dictionary_policy": "field_scoped_scope_predicate_vocabulary_only",
            },
        },
        "local_candidates": {
            "entity_hits": [{"entity": "domains", "matched_tokens": ["domains"]}],
            "field_hits": [
                {
                    "field": "domains.is_spf_verified",
                    "role": "boolean",
                    "matched_tokens": ["spf", "verified"],
                }
            ],
            "value_dictionary_hits": [],
            "ambiguous_physical_families_mentioned": [],
        },
        "query_frame": {
            "schema_version": 3,
            "source": "query_frame_error",
            "stage_pinned": "needs_model",
        },
        "allowed_resolution_contract": {
            "llm_output": "resolution_proposal_json",
            "must_not_emit_final_sql": True,
            "must_reference_schema_card_entities_and_fields": True,
            "value_filters_should_use_schema_card_value_dictionary": True,
            "must_ask_clarifying_questions_on_ambiguity": True,
            "semsql_must_validate_before_execution": True,
        },
    }


def _domains_spf_rate_proposal() -> dict[str, object]:
    return {
        "schema_version": 1,
        "action": "route",
        "confidence": 0.92,
        "intent": "percentage of domains that are SPF verified",
        "distinct": False,
        "target_entities": ["domains"],
        "projections": [
            {
                "kind": "conditional_rate",
                "field": "",
                "aggregate": "",
                "alias": "spf_verified_rate",
                "numerator_field": "domains.is_spf_verified",
                "numerator_operator": "=",
                "numerator_value": True,
                "numerator_value_kind": "literal",
                "denominator_field": "domains.id",
                "scale": 100.0,
                "rationale": "SPF verification is stored as a boolean on domains.",
            }
        ],
        "filters": [],
        "joins": [],
        "group_by": [],
        "order_by": [],
        "limit": 100,
        "ambiguity_questions": [],
        "evidence": [
            {
                "claim": "domains.is_spf_verified is in the packet",
                "graph_refs": ["domains.is_spf_verified"],
            }
        ],
        "safety_notes": [],
    }


def _write_recovery_report_fixture(tmp_path: Path) -> tuple[Path, Path]:
    packet_dir = tmp_path / "packets" / "case-001"
    packet_dir.mkdir(parents=True)
    packet_json = packet_dir / "rejected.packet.json"
    packet_json.write_text(
        json.dumps(_domains_rate_rejection_packet()),
        encoding="utf-8",
    )
    report_json = tmp_path / "realdb-report.json"
    report_json.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": "realdb_typed_fallback_mysql_suite",
                "runs": [
                    {
                        "seed": 20260604,
                        "database": "mailer_web",
                        "records": [
                            {
                                "index": 1,
                                "question": (
                                    "what percentage of domains are SPF verified?"
                                ),
                                "expected_kind": "conditional_rate",
                                "expected_table": "domains",
                                "expected_field": "is_spf_verified",
                                "selected_sql": "",
                                "artifacts": {"packet": str(packet_json)},
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return report_json, packet_json


def test_llm_resolution_resolve_packet_selects_saved_typed_proposal(
    tmp_path: Path,
) -> None:
    from semsql_eval.__main__ import cli

    graph = tmp_path / "g.semsql"
    _make_semantic_graph(graph)
    packet_json = tmp_path / "rejected.packet.json"
    proposal_json = tmp_path / "proposal.json"
    summary_json = tmp_path / "summary.json"
    summary_md = tmp_path / "summary.md"
    render_json = tmp_path / "render.json"
    packet_json.write_text(
        json.dumps(_rust_style_rejection_packet(graph)),
        encoding="utf-8",
    )
    proposal_json.write_text(json.dumps(_mail_subject_proposal()), encoding="utf-8")

    result = CliRunner().invoke(
        cli,
        [
            "llm-resolution-resolve-packet",
            "--packet-json",
            str(packet_json),
            "--proposal-json",
            str(proposal_json),
            "--render-out",
            str(render_json),
            "--out-json",
            str(summary_json),
            "--out-md",
            str(summary_md),
            "--strict",
        ],
    )

    assert result.exit_code == 0, result.output
    summary = json.loads(summary_json.read_text(encoding="utf-8"))
    render = json.loads(render_json.read_text(encoding="utf-8"))
    assert summary["status"] == "selected"
    assert summary["selected_source"] == "typed_proposal"
    assert summary["provider_call_count"] == 0
    assert summary["used_direct_llm_sql"] is False
    assert summary["render_valid"] is True
    assert summary["artifacts"]["packet"] == str(packet_json)
    assert summary["artifacts"]["render"] == str(render_json)
    assert render["valid"] is True
    assert 'SELECT "mails"."subject" FROM "mails"' in summary["selected_sql"]
    assert summary["result_shape"]["kind"] == "table"
    assert "direct LLM SQL used: `False`" in summary_md.read_text(encoding="utf-8")


def test_llm_resolution_resolve_packet_executes_sqlite_readonly_preview(
    tmp_path: Path,
) -> None:
    from semsql_eval.__main__ import cli

    graph = tmp_path / "g.semsql"
    _make_semantic_graph(graph)
    db_path = tmp_path / "mail.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE mails (subject TEXT);
        INSERT INTO mails VALUES ('Welcome'), ('Renewal');
        """
    )
    conn.close()
    packet_json = tmp_path / "rejected.packet.json"
    proposal_json = tmp_path / "proposal.json"
    summary_json = tmp_path / "summary.json"
    execution_json = tmp_path / "execution.json"
    packet_json.write_text(
        json.dumps(_rust_style_rejection_packet(graph)),
        encoding="utf-8",
    )
    proposal_json.write_text(json.dumps(_mail_subject_proposal()), encoding="utf-8")

    result = CliRunner().invoke(
        cli,
        [
            "llm-resolution-resolve-packet",
            "--packet-json",
            str(packet_json),
            "--proposal-json",
            str(proposal_json),
            "--execute-sqlite",
            str(db_path),
            "--execution-out",
            str(execution_json),
            "--out-json",
            str(summary_json),
            "--max-rows",
            "1",
            "--strict",
        ],
    )

    assert result.exit_code == 0, result.output
    summary = json.loads(summary_json.read_text(encoding="utf-8"))
    execution = json.loads(execution_json.read_text(encoding="utf-8"))
    assert summary["execution"]["status"] == "ok"
    assert summary["execution"]["rows"] == [["Welcome"]]
    assert summary["execution"]["truncated"] is True
    assert summary["artifacts"]["execution"] == str(execution_json)
    assert execution["columns"] == ["subject"]
    assert execution["row_count_preview"] == 1
    assert "## Execution" in result.output
    assert "| subject |" in result.output


def test_llm_resolution_resolve_packet_can_discard_execution_rows(
    tmp_path: Path,
) -> None:
    from semsql_eval.__main__ import cli

    graph = tmp_path / "g.semsql"
    _make_semantic_graph(graph)
    db_path = tmp_path / "mail.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE mails (subject TEXT);
        INSERT INTO mails VALUES ('Welcome'), ('Renewal');
        """
    )
    conn.close()
    packet_json = tmp_path / "rejected.packet.json"
    proposal_json = tmp_path / "proposal.json"
    summary_json = tmp_path / "summary.json"
    execution_json = tmp_path / "execution.json"
    packet_json.write_text(
        json.dumps(_rust_style_rejection_packet(graph)),
        encoding="utf-8",
    )
    proposal_json.write_text(json.dumps(_mail_subject_proposal()), encoding="utf-8")

    result = CliRunner().invoke(
        cli,
        [
            "llm-resolution-resolve-packet",
            "--packet-json",
            str(packet_json),
            "--proposal-json",
            str(proposal_json),
            "--execute-sqlite",
            str(db_path),
            "--execution-out",
            str(execution_json),
            "--max-rows",
            "1",
            "--discard-execution-rows",
            "--strict",
            "--out-json",
            str(summary_json),
        ],
    )

    assert result.exit_code == 0, result.output
    summary = json.loads(summary_json.read_text(encoding="utf-8"))
    execution = json.loads(execution_json.read_text(encoding="utf-8"))
    assert summary["execution"]["status"] == "ok"
    assert summary["execution"]["columns"] == ["subject"]
    assert summary["execution"]["row_count_preview"] == 1
    assert summary["execution"]["rows"] == []
    assert summary["execution"]["rows_retained"] is False
    assert execution["rows"] == []
    assert "rows retained: `False`" in result.output
    assert "### Result Preview" not in result.output


def test_llm_resolution_resolve_packet_can_use_mocked_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from semsql_eval.__main__ import cli

    graph = tmp_path / "g.semsql"
    _make_semantic_graph(graph)
    packet_json = tmp_path / "rejected.packet.json"
    provider_json = tmp_path / "provider.json"
    proposal_json = tmp_path / "proposal.json"
    render_json = tmp_path / "render.json"
    packet_json.write_text(
        json.dumps(_rust_style_rejection_packet(graph)),
        encoding="utf-8",
    )

    def fake_call_openai_resolution(
        packet: dict[str, object],
        *,
        model: str | None = None,
    ) -> dict[str, object]:
        assert packet["source"] == "semsql_rejected_query_packet"
        assert model == "gpt-test"
        return {
            "schema_version": 1,
            "source": "fake_openai",
            "model": model,
            "proposal": _mail_subject_proposal(),
        }

    monkeypatch.setattr(
        "semsql_eval.__main__.call_openai_resolution",
        fake_call_openai_resolution,
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    result = CliRunner().invoke(
        cli,
        [
            "llm-resolution-resolve-packet",
            "--packet-json",
            str(packet_json),
            "--provider",
            "openai",
            "--model",
            "gpt-test",
            "--provider-out",
            str(provider_json),
            "--proposal-out",
            str(proposal_json),
            "--render-out",
            str(render_json),
            "--strict",
        ],
    )

    assert result.exit_code == 0, result.output
    provider_result = json.loads(provider_json.read_text(encoding="utf-8"))
    proposal = json.loads(proposal_json.read_text(encoding="utf-8"))
    render = json.loads(render_json.read_text(encoding="utf-8"))
    assert provider_result["source"] == "fake_openai"
    assert proposal["action"] == "route"
    assert render["valid"] is True
    assert "provider calls: `1`" in result.output
    assert "direct LLM SQL used: `False`" in result.output


def test_llm_resolution_resolve_packet_can_use_mocked_groq_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from semsql_eval.__main__ import cli

    graph = tmp_path / "g.semsql"
    _make_semantic_graph(graph)
    packet_json = tmp_path / "rejected.packet.json"
    provider_json = tmp_path / "provider.json"
    render_json = tmp_path / "render.json"
    packet_json.write_text(
        json.dumps(_rust_style_rejection_packet(graph)),
        encoding="utf-8",
    )

    def fake_call_typed_resolution_provider(
        packet: dict[str, object],
        *,
        provider: str,
        model: str | None,
        provider_base_url: str | None,
        provider_api_key_env: str | None,
    ) -> dict[str, object]:
        assert packet["source"] == "semsql_rejected_query_packet"
        assert provider == "groq"
        assert model == "llama-test"
        assert provider_base_url is None
        assert provider_api_key_env is None
        return {
            "schema_version": 1,
            "source": "fake_groq",
            "model": model,
            "proposal": _mail_subject_proposal(),
        }

    monkeypatch.setattr(
        "semsql_eval.__main__._call_typed_resolution_provider",
        fake_call_typed_resolution_provider,
    )
    monkeypatch.setenv("GROQ_API_KEY", "test-key")

    result = CliRunner().invoke(
        cli,
        [
            "llm-resolution-resolve-packet",
            "--packet-json",
            str(packet_json),
            "--provider",
            "groq",
            "--model",
            "llama-test",
            "--provider-out",
            str(provider_json),
            "--render-out",
            str(render_json),
            "--strict",
        ],
    )

    assert result.exit_code == 0, result.output
    provider_result = json.loads(provider_json.read_text(encoding="utf-8"))
    render = json.loads(render_json.read_text(encoding="utf-8"))
    assert provider_result["source"] == "fake_groq"
    assert render["valid"] is True
    assert "provider calls: `1`" in result.output
    assert "direct LLM SQL used: `False`" in result.output


def test_llm_resolution_resolve_packet_missing_provider_env_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from semsql_eval.__main__ import cli

    graph = tmp_path / "g.semsql"
    _make_semantic_graph(graph)
    packet_json = tmp_path / "rejected.packet.json"
    summary_json = tmp_path / "summary.json"
    packet_json.write_text(
        json.dumps(_rust_style_rejection_packet(graph)),
        encoding="utf-8",
    )

    def provider_must_not_run(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("provider call should be skipped when env is missing")

    monkeypatch.setattr(
        "semsql_eval.__main__.call_openai_resolution",
        provider_must_not_run,
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    result = CliRunner().invoke(
        cli,
        [
            "llm-resolution-resolve-packet",
            "--packet-json",
            str(packet_json),
            "--provider",
            "openai",
            "--out-json",
            str(summary_json),
        ],
    )

    assert result.exit_code == 0, result.output
    summary = json.loads(summary_json.read_text(encoding="utf-8"))
    assert summary["status"] == "unresolved"
    assert summary["provider_called"] is False
    assert summary["provider_call_count"] == 0
    assert summary["provider_readiness"] == {
        "provider": "openai",
        "configured": False,
        "missing_env": ["OPENAI_API_KEY"],
        "skipped_reason": "provider_not_configured: OPENAI_API_KEY is not set",
    }
    assert summary["provider_error"] == "provider_not_configured: OPENAI_API_KEY is not set"
    assert "provider configured: `False`" in result.output


def test_realdb_typed_fallback_recover_report_missing_provider_env_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from semsql_eval.__main__ import cli

    report_json, _packet_json = _write_recovery_report_fixture(tmp_path)
    out_json = tmp_path / "recovery.json"

    def provider_must_not_run(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("provider call should be skipped when env is missing")

    monkeypatch.setattr(
        "semsql_eval.__main__._call_typed_resolution_provider",
        provider_must_not_run,
    )
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    result = CliRunner().invoke(
        cli,
        [
            "realdb-typed-fallback-recover-report",
            "--report-json",
            str(report_json),
            "--out",
            str(tmp_path / "recovery"),
            "--provider",
            "groq",
            "--out-json",
            str(out_json),
        ],
    )

    assert result.exit_code == 0, result.output
    report = json.loads(out_json.read_text(encoding="utf-8"))
    record = report["records"][0]
    assert report["summary"]["unresolved_input_count"] == 1
    assert report["summary"]["selected"] == 0
    assert report["summary"]["provider_call_count"] == 0
    assert report["summary"]["provider_errors"] == 1
    assert report["summary"]["direct_llm_sql_count"] == 0
    assert report["summary"]["pass"] is False
    assert record["selected_source"] is None
    assert record["provider_readiness"]["configured"] is False
    assert record["provider_readiness"]["missing_env"] == ["GROQ_API_KEY"]
    assert record["provider_error"] == (
        "provider_not_configured: GROQ_API_KEY is not set"
    )
    assert "provider errors: `1`" in result.output


def test_realdb_typed_fallback_recover_report_uses_reviewed_proposal(
    tmp_path: Path,
) -> None:
    from semsql_eval.__main__ import cli

    report_json, _packet_json = _write_recovery_report_fixture(tmp_path)
    proposal_dir = tmp_path / "reviewed-proposals"
    proposal_dir.mkdir()
    proposal_path = proposal_dir / "seed-20260604--case-001.proposal.json"
    proposal_path.write_text(
        json.dumps(_domains_spf_rate_proposal()),
        encoding="utf-8",
    )
    out_json = tmp_path / "recovery.json"

    result = CliRunner().invoke(
        cli,
        [
            "realdb-typed-fallback-recover-report",
            "--report-json",
            str(report_json),
            "--out",
            str(tmp_path / "recovery"),
            "--provider",
            "none",
            "--proposal-dir",
            str(proposal_dir),
            "--out-json",
            str(out_json),
            "--strict",
        ],
    )

    assert result.exit_code == 0, result.output
    report = json.loads(out_json.read_text(encoding="utf-8"))
    record = report["records"][0]
    assert report["summary"]["unresolved_input_count"] == 1
    assert report["summary"]["selected"] == 1
    assert report["summary"]["selected_sources"] == {"typed_proposal": 1}
    assert report["summary"]["provider_call_count"] == 0
    assert report["summary"]["provider_errors"] == 0
    assert report["summary"]["render_errors"] == 0
    assert report["summary"]["expected_matches"] == 1
    assert report["summary"]["rows_retained_cases"] == 0
    assert report["summary"]["direct_llm_sql_count"] == 0
    assert report["summary"]["pass"] is True
    assert record["proposal"] == str(proposal_path)
    assert record["selected_source"] == "typed_proposal"
    assert record["render_valid"] is True
    assert record["expected_match"] is True
    assert record["used_direct_llm_sql"] is False
    assert "SUM(CASE WHEN" in record["selected_sql"]
    assert "`domains`.`is_spf_verified`" in record["selected_sql"]
    assert record["artifacts"]["provider_result"] is None
    assert record["artifacts"]["proposal"] is not None
    assert record["artifacts"]["render"] is not None
    assert "pass: `True`" in result.output


def test_llm_resolution_resolve_batch_missing_provider_env_fails_fast(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from semsql_eval.__main__ import cli

    graph = tmp_path / "g.semsql"
    _make_semantic_graph(graph)
    packet_dir = tmp_path / "packets"
    packet_dir.mkdir()
    (packet_dir / "case-001.packet.json").write_text(
        json.dumps(_rust_style_rejection_packet(graph)),
        encoding="utf-8",
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    result = CliRunner().invoke(
        cli,
        [
            "llm-resolution-resolve-batch",
            "--packet-dir",
            str(packet_dir),
            "--provider",
            "openai",
        ],
    )

    assert result.exit_code != 0
    assert "provider_not_configured: OPENAI_API_KEY is not set" in result.output


def test_llm_resolution_resolve_packet_accepts_rust_emitted_packet(
    tmp_path: Path,
    semsql_bin: Path,
) -> None:
    from semsql_eval.__main__ import cli

    graph = tmp_path / "g.semsql"
    _make_semantic_graph(graph)
    packet_json = tmp_path / "rejected.packet.json"
    proposal_json = tmp_path / "proposal.json"
    render_json = tmp_path / "render.json"
    proposal_json.write_text(json.dumps(_mail_subject_proposal()), encoding="utf-8")

    proc = subprocess.run(
        [
            str(semsql_bin),
            "query",
            "--graph",
            str(graph),
            "--rejection-packet-json",
            str(packet_json),
            "which customer is healthiest?",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode != 0
    assert packet_json.exists()
    packet = json.loads(packet_json.read_text(encoding="utf-8"))
    assert packet["source"] == "semsql_rejected_query_packet"
    assert packet["allowed_resolution_contract"]["must_not_emit_final_sql"] is True
    assert packet["schema_card"]["safety"]["llm_may_not_execute_sql"] is True

    result = CliRunner().invoke(
        cli,
        [
            "llm-resolution-resolve-packet",
            "--packet-json",
            str(packet_json),
            "--proposal-json",
            str(proposal_json),
            "--render-out",
            str(render_json),
            "--strict",
        ],
    )

    assert result.exit_code == 0, result.output
    render = json.loads(render_json.read_text(encoding="utf-8"))
    assert render["valid"] is True
    assert 'SELECT "mails"."subject" FROM "mails"' in render["sql"]
    assert "direct LLM SQL used: `False`" in result.output


def test_llm_resolution_resolve_packet_result_shape_maps_grouped_chart() -> None:
    from semsql_eval.__main__ import _result_shape_hint

    shape = _result_shape_hint(
        'SELECT "campaigns"."name", COUNT("leads"."id") AS "lead_count" '
        'FROM "leads" JOIN "campaigns" ON "leads"."campaign_id" = "campaigns"."id" '
        'GROUP BY "campaigns"."name"'
    )
    chartjs = cast(dict[str, Any], shape["chartjs"])
    mapping = cast(dict[str, Any], chartjs["mapping"])

    assert shape["kind"] == "categorical_chart"
    assert shape["default_view"] == "chart"
    assert chartjs["type"] == "bar"
    assert mapping["labels_from"] == "name"


def test_llm_resolution_result_shape_maps_multi_series_chart() -> None:
    from semsql_eval.__main__ import _result_shape_hint

    shape = _result_shape_hint(
        'SELECT DATE("accounts"."created_at") AS "day", '
        '"regions"."name" AS "region", COUNT(*) AS "account_count" '
        'FROM "accounts" JOIN "regions" ON "regions"."id" = "accounts"."region_id" '
        'GROUP BY DATE("accounts"."created_at"), "regions"."name"'
    )
    chartjs = cast(dict[str, Any], shape["chartjs"])
    mapping = cast(dict[str, Any], chartjs["mapping"])

    assert shape["kind"] == "multi_series_chart"
    assert shape["default_view"] == "chart"
    assert chartjs["type"] == "line"
    assert mapping["labels_from"] == "day"
    assert mapping["series_from"] == "region"
    assert mapping["datasets"][0]["data_from"] == "account_count"


def test_llm_resolution_resolve_packet_execution_rejects_multi_statement(
    tmp_path: Path,
) -> None:
    from semsql_eval.__main__ import _execute_selected_sqlite

    db_path = tmp_path / "mail.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript("CREATE TABLE mails (subject TEXT); INSERT INTO mails VALUES ('Welcome');")
    conn.close()

    execution = _execute_selected_sqlite(
        db_path,
        "SELECT subject FROM mails; DROP TABLE mails",
        max_rows=10,
        timeout_seconds=1.0,
    )

    assert execution["status"] == "error"
    assert "exactly one SQL statement" in str(execution["error"])


def test_llm_resolution_execute_db_url_sqlite_preview(tmp_path: Path) -> None:
    from semsql_eval.__main__ import _execute_selected_db_url

    db_path = tmp_path / "mail.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript("CREATE TABLE mails (subject TEXT); INSERT INTO mails VALUES ('Welcome');")
    conn.close()

    execution = _execute_selected_db_url(
        f"sqlite:///{db_path.as_posix()}",
        "SELECT subject FROM mails",
        dialect="sqlite",
        max_rows=10,
        timeout_seconds=1.0,
    )

    assert execution["engine"] == "sqlite"
    assert execution["status"] == "ok"
    assert execution["rows"] == [["Welcome"]]


class _FakeCursor:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.description = [("count",)]

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, *_args: object) -> None:
        self.calls.append(sql)

    def fetchmany(self, _limit: int) -> list[tuple[int]]:
        return [(2,)]


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self.cursor_obj = cursor
        self.rollback_count = 0
        self.closed = False
        self.autocommit = True

    def cursor(self) -> _FakeCursor:
        return self.cursor_obj

    def rollback(self) -> None:
        self.rollback_count += 1

    def close(self) -> None:
        self.closed = True


def test_llm_resolution_execute_db_url_mysql_uses_readonly_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from semsql_eval.__main__ import _execute_selected_db_url

    cursor = _FakeCursor()
    conn = _FakeConnection(cursor)
    connect_kwargs: dict[str, object] = {}

    class FakePyMySql:
        @staticmethod
        def connect(**kwargs: object) -> _FakeConnection:
            connect_kwargs.update(kwargs)
            return conn

    monkeypatch.setitem(sys.modules, "pymysql", FakePyMySql)

    execution = _execute_selected_db_url(
        "mariadb://root:password@localhost:3306/app_db",
        "SELECT COUNT(*) AS count FROM mails",
        dialect="mysql",
        max_rows=10,
        timeout_seconds=1.0,
    )

    assert execution["engine"] == "mariadb"
    assert execution["status"] == "ok"
    assert execution["rows"] == [[2]]
    assert execution["target"] == "mariadb://root:***@localhost:3306/app_db"
    assert connect_kwargs["database"] == "app_db"
    assert "START TRANSACTION READ ONLY" in cursor.calls
    assert conn.rollback_count >= 1
    assert conn.closed is True


def test_llm_resolution_execute_db_url_can_discard_mysql_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from semsql_eval.__main__ import _execute_selected_db_url

    cursor = _FakeCursor()
    conn = _FakeConnection(cursor)

    class FakePyMySql:
        @staticmethod
        def connect(**_kwargs: object) -> _FakeConnection:
            return conn

    monkeypatch.setitem(sys.modules, "pymysql", FakePyMySql)

    execution = _execute_selected_db_url(
        "mariadb://root:password@localhost:3306/app_db",
        "SELECT COUNT(*) AS count FROM mails",
        dialect="mysql",
        max_rows=10,
        retain_rows=False,
        timeout_seconds=1.0,
    )

    assert execution["engine"] == "mariadb"
    assert execution["status"] == "ok"
    assert execution["columns"] == ["count"]
    assert execution["row_count_preview"] == 1
    assert execution["rows"] == []
    assert execution["rows_retained"] is False
    assert "START TRANSACTION READ ONLY" in cursor.calls


def test_llm_resolution_execute_db_url_postgres_uses_readonly_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from semsql_eval.__main__ import _execute_selected_db_url

    cursor = _FakeCursor()
    conn = _FakeConnection(cursor)

    class FakePsycopg:
        @staticmethod
        def connect(*_args: object, **_kwargs: object) -> _FakeConnection:
            return conn

    monkeypatch.setitem(sys.modules, "psycopg", FakePsycopg)

    execution = _execute_selected_db_url(
        "postgres://reporter:secret@db.example/app_db",
        "SELECT COUNT(*) AS count FROM mails",
        dialect="postgres",
        max_rows=10,
        timeout_seconds=1.0,
    )

    assert execution["engine"] == "postgres"
    assert execution["status"] == "ok"
    assert execution["rows"] == [[2]]
    assert execution["target"] == "postgres://reporter:***@db.example/app_db"
    assert "BEGIN READ ONLY" in cursor.calls
    assert any(call.startswith("SET LOCAL statement_timeout") for call in cursor.calls)
    assert conn.autocommit is False


def test_llm_resolution_fallback_batch_selects_validated_typed_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from semsql_eval.__main__ import cli

    graph = tmp_path / "g.semsql"
    _make_semantic_graph(graph)
    packet_dir = tmp_path / "packets"
    packet_dir.mkdir()
    packet_json = packet_dir / "case-001.packet.json"
    packet_json.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": "semsql_rejected_query_packet",
                "question": "show mail subjects",
                "schema_card": {"graph": str(graph)},
            }
        ),
        encoding="utf-8",
    )
    (packet_dir / "case-001.proposal.json").write_text(
        json.dumps(_mail_subject_proposal()),
        encoding="utf-8",
    )
    out_dir = tmp_path / "fallback-batch"

    def fake_run_cascade_query(*_args: object, **kwargs: object) -> SimpleNamespace:
        frame_path = kwargs["query_frame_json"]
        assert isinstance(frame_path, Path)
        frame_path.write_text('{"intent":"batch"}\n', encoding="utf-8")
        return SimpleNamespace(
            sql=None,
            stage_pinned="needs_model",
            error_detail="needs model",
            elapsed_seconds=0.01,
            query_frame={"intent": "batch"},
        )

    monkeypatch.setattr(
        "semsql_eval.__main__.run_cascade_query",
        fake_run_cascade_query,
    )

    result = CliRunner().invoke(
        cli,
        [
            "llm-resolution-fallback-batch",
            "--packet-dir",
            str(packet_dir),
            "--out",
            str(out_dir),
            "--strict",
        ],
    )

    assert result.exit_code == 0, result.output
    summary = json.loads((out_dir / "fallback-batch.json").read_text(encoding="utf-8"))
    case_summary = json.loads(
        (out_dir / "case-001" / "fallback-query.json").read_text(encoding="utf-8")
    )
    assert summary["packet_count"] == 1
    assert summary["selected_count"] == 1
    assert summary["typed_fallback_selected_count"] == 1
    assert summary["provider_call_count"] == 0
    assert summary["direct_llm_sql_count"] == 0
    assert summary["fallback_render_valid_count"] == 1
    assert summary["cases"][0]["selected_source"] == "typed_fallback"
    assert case_summary["selected_source"] == "typed_fallback"
    assert 'SELECT "mails"."subject" FROM "mails"' in case_summary["selected_sql"]


def test_llm_resolution_fallback_batch_applies_clarification_choice_map(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from semsql_eval.__main__ import cli

    graph = tmp_path / "g.semsql"
    _make_semantic_graph(graph)
    conn = sqlite3.connect(graph)
    conn.executescript(
        """
        INSERT INTO entities(canonical_name, db_table, singular_label, plural_label)
        VALUES ('organizations', 'organizations', 'organization', 'organizations');
        INSERT INTO fields(entity, field, db_column, type, display_label)
        VALUES
          ('organizations', 'id', 'id', 'integer', 'ID'),
          ('organizations', 'name', 'name', 'text', 'Name'),
          ('mails', 'organization_id', 'organization_id', 'integer', 'Organization ID'),
          ('mails', 'owner_organization_id', 'owner_organization_id', 'integer', 'Owner Organization ID');
        INSERT INTO relationships(from_entity, from_field, to_entity, to_field, kind)
        VALUES
          ('mails', 'organization_id', 'organizations', 'id', 'many_to_one'),
          ('mails', 'owner_organization_id', 'organizations', 'id', 'many_to_one');
        """
    )
    conn.close()
    packet_dir = tmp_path / "packets"
    proposal_dir = tmp_path / "proposals"
    packet_dir.mkdir()
    proposal_dir.mkdir()
    (packet_dir / "case-001.packet.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": "semsql_rejected_query_packet",
                "question": "count mails by organization",
                "schema_card": {"graph": str(graph)},
            }
        ),
        encoding="utf-8",
    )
    proposal = _mail_subject_proposal()
    proposal.update(
        {
            "action": "clarify",
            "result_shape": "categorical_chart",
            "target_entities": ["mails", "organizations"],
            "projections": [
                {
                    "kind": "count",
                    "field": "",
                    "aggregate": "COUNT",
                    "alias": "mail_count",
                    "rationale": "count mails",
                },
                {
                    "kind": "field",
                    "field": "organizations.name",
                    "aggregate": "",
                    "alias": "organization",
                    "rationale": "display organization",
                },
            ],
            "group_by": ["organizations.name"],
            "ambiguity_questions": [
                "Which organization path should be used: mails.organization_id or mails.owner_organization_id?"
            ],
        }
    )
    (proposal_dir / "case-001.proposal.json").write_text(
        json.dumps(proposal),
        encoding="utf-8",
    )
    choices = tmp_path / "choices.json"
    choices.write_text(json.dumps({"case-001": "schema_path_2"}), encoding="utf-8")
    out_dir = tmp_path / "fallback-batch-choice"

    def fake_run_cascade_query(*_args: object, **kwargs: object) -> SimpleNamespace:
        frame_path = kwargs["query_frame_json"]
        assert isinstance(frame_path, Path)
        frame_path.write_text('{"intent":"batch"}\n', encoding="utf-8")
        return SimpleNamespace(
            sql=None,
            stage_pinned="needs_model",
            error_detail="needs model",
            elapsed_seconds=0.01,
            query_frame={"intent": "batch"},
        )

    monkeypatch.setattr(
        "semsql_eval.__main__.run_cascade_query",
        fake_run_cascade_query,
    )

    result = CliRunner().invoke(
        cli,
        [
            "llm-resolution-fallback-batch",
            "--packet-dir",
            str(packet_dir),
            "--proposal-dir",
            str(proposal_dir),
            "--clarification-choices-json",
            str(choices),
            "--out",
            str(out_dir),
            "--strict",
        ],
    )

    assert result.exit_code == 0, result.output
    summary = json.loads((out_dir / "fallback-batch.json").read_text(encoding="utf-8"))
    case_summary = json.loads(
        (out_dir / "case-001" / "fallback-query.json").read_text(encoding="utf-8")
    )
    assert summary["selected_count"] == 1
    assert summary["unresolved_count"] == 0
    assert summary["clarification_choice_count"] == 1
    assert summary["cases"][0]["clarification_choice"] == "schema_path_2"
    assert case_summary["clarification_choice"] == "schema_path_2"
    assert (
        '"mails"."owner_organization_id" = "organizations"."id"'
        in case_summary["selected_sql"]
    )


def test_llm_resolution_fallback_batch_executes_selected_sql_with_rows_discarded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from semsql_eval.__main__ import cli

    graph = tmp_path / "g.semsql"
    _make_semantic_graph(graph)
    db_path = tmp_path / "mail.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE mails (subject TEXT);
        INSERT INTO mails VALUES ('Welcome'), ('Receipt');
        """
    )
    conn.close()

    packet_dir = tmp_path / "packets"
    proposal_dir = tmp_path / "proposals"
    packet_dir.mkdir()
    proposal_dir.mkdir()
    (packet_dir / "case-001.packet.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": "semsql_rejected_query_packet",
                "question": "show mail subjects",
                "schema_card": {"graph": str(graph)},
            }
        ),
        encoding="utf-8",
    )
    (proposal_dir / "case-001.proposal.json").write_text(
        json.dumps(_mail_subject_proposal()),
        encoding="utf-8",
    )
    out_dir = tmp_path / "fallback-batch-exec"

    def fake_run_cascade_query(*_args: object, **kwargs: object) -> SimpleNamespace:
        frame_path = kwargs["query_frame_json"]
        assert isinstance(frame_path, Path)
        frame_path.write_text('{"intent":"batch"}\n', encoding="utf-8")
        return SimpleNamespace(
            sql=None,
            stage_pinned="needs_model",
            error_detail="needs model",
            elapsed_seconds=0.01,
            query_frame={"intent": "batch"},
        )

    monkeypatch.setattr(
        "semsql_eval.__main__.run_cascade_query",
        fake_run_cascade_query,
    )

    result = CliRunner().invoke(
        cli,
        [
            "llm-resolution-fallback-batch",
            "--packet-dir",
            str(packet_dir),
            "--proposal-dir",
            str(proposal_dir),
            "--execute-sqlite",
            str(db_path),
            "--discard-execution-rows",
            "--out",
            str(out_dir),
            "--strict",
        ],
    )

    assert result.exit_code == 0, result.output
    summary = json.loads((out_dir / "fallback-batch.json").read_text(encoding="utf-8"))
    case_summary = json.loads(
        (out_dir / "case-001" / "fallback-query.json").read_text(encoding="utf-8")
    )
    execution = case_summary["execution"]
    assert summary["execution_requested"] is True
    assert summary["execution_ok_count"] == 1
    assert summary["execution_error_count"] == 0
    assert summary["execution_skipped_count"] == 0
    assert summary["rows_retained_cases"] == 0
    assert summary["cases"][0]["execution_status"] == "ok"
    assert summary["cases"][0]["execution_row_count_preview"] == 2
    assert execution["status"] == "ok"
    assert execution["rows"] == []
    assert execution["rows_retained"] is False


def test_llm_resolution_fallback_batch_executes_with_per_case_db_url_map(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from semsql_eval.__main__ import cli

    graph = tmp_path / "g.semsql"
    _make_semantic_graph(graph)
    db_path = tmp_path / "mail.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE mails (subject TEXT);
        INSERT INTO mails VALUES ('Welcome');
        """
    )
    conn.close()

    packet_dir = tmp_path / "packets"
    proposal_dir = tmp_path / "proposals"
    packet_dir.mkdir()
    proposal_dir.mkdir()
    (packet_dir / "case-001.packet.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": "semsql_rejected_query_packet",
                "question": "show mail subjects",
                "schema_card": {"graph": str(graph)},
            }
        ),
        encoding="utf-8",
    )
    (proposal_dir / "case-001.proposal.json").write_text(
        json.dumps(_mail_subject_proposal()),
        encoding="utf-8",
    )
    execution_map = tmp_path / "execution-map.json"
    execution_map.write_text(
        json.dumps({"case-001": f"sqlite:///{db_path.resolve().as_posix()}"}),
        encoding="utf-8",
    )
    out_dir = tmp_path / "fallback-batch-exec-map"

    def fake_run_cascade_query(*_args: object, **kwargs: object) -> SimpleNamespace:
        frame_path = kwargs["query_frame_json"]
        assert isinstance(frame_path, Path)
        frame_path.write_text('{"intent":"batch"}\n', encoding="utf-8")
        return SimpleNamespace(
            sql=None,
            stage_pinned="needs_model",
            error_detail="needs model",
            elapsed_seconds=0.01,
            query_frame={"intent": "batch"},
        )

    monkeypatch.setattr(
        "semsql_eval.__main__.run_cascade_query",
        fake_run_cascade_query,
    )

    result = CliRunner().invoke(
        cli,
        [
            "llm-resolution-fallback-batch",
            "--packet-dir",
            str(packet_dir),
            "--proposal-dir",
            str(proposal_dir),
            "--execute-db-url-json",
            str(execution_map),
            "--discard-execution-rows",
            "--out",
            str(out_dir),
            "--strict",
        ],
    )

    assert result.exit_code == 0, result.output
    summary = json.loads((out_dir / "fallback-batch.json").read_text(encoding="utf-8"))
    case_summary = json.loads(
        (out_dir / "case-001" / "fallback-query.json").read_text(encoding="utf-8")
    )
    assert summary["execution_target"] == "per-case-db-url-map"
    assert summary["execution_db_url_map_count"] == 1
    assert summary["execution_ok_count"] == 1
    assert summary["cases"][0]["execution_status"] == "ok"
    assert summary["cases"][0]["execution_target"].startswith("sqlite:///")
    assert case_summary["execution"]["execution_source"] == "db_url"
    assert case_summary["execution"]["rows_retained"] is False


def test_llm_resolution_fallback_batch_buckets_execution_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from semsql_eval.__main__ import cli

    graph = tmp_path / "g.semsql"
    _make_semantic_graph(graph)
    db_path = tmp_path / "wrong.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE unrelated (id INTEGER);
        """
    )
    conn.close()

    packet_dir = tmp_path / "packets"
    proposal_dir = tmp_path / "proposals"
    packet_dir.mkdir()
    proposal_dir.mkdir()
    (packet_dir / "case-001.packet.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": "semsql_rejected_query_packet",
                "question": "show mail subjects",
                "schema_card": {"graph": str(graph)},
            }
        ),
        encoding="utf-8",
    )
    (proposal_dir / "case-001.proposal.json").write_text(
        json.dumps(_mail_subject_proposal()),
        encoding="utf-8",
    )
    out_dir = tmp_path / "fallback-batch-exec-failure"

    def fake_run_cascade_query(*_args: object, **kwargs: object) -> SimpleNamespace:
        frame_path = kwargs["query_frame_json"]
        assert isinstance(frame_path, Path)
        frame_path.write_text('{"intent":"batch"}\n', encoding="utf-8")
        return SimpleNamespace(
            sql=None,
            stage_pinned="needs_model",
            error_detail="needs model",
            elapsed_seconds=0.01,
            query_frame={"intent": "batch"},
        )

    monkeypatch.setattr(
        "semsql_eval.__main__.run_cascade_query",
        fake_run_cascade_query,
    )

    result = CliRunner().invoke(
        cli,
        [
            "llm-resolution-fallback-batch",
            "--packet-dir",
            str(packet_dir),
            "--proposal-dir",
            str(proposal_dir),
            "--execute-sqlite",
            str(db_path),
            "--discard-execution-rows",
            "--out",
            str(out_dir),
            "--strict",
        ],
    )

    assert result.exit_code != 0
    assert "execution_schema_missing=1" in result.output
    assert "fallback batch execution did not pass" in result.output
    summary = json.loads((out_dir / "fallback-batch.json").read_text(encoding="utf-8"))
    assert summary["selected_count"] == 1
    assert summary["execution_ok_count"] == 0
    assert summary["execution_error_count"] == 1
    assert summary["execution_failure_buckets"] == {"execution_schema_missing": 1}
    assert summary["execution_failure_cases"][0]["stem"] == "case-001"
    assert summary["cases"][0]["execution_status"] == "error"
    assert summary["cases"][0]["execution_failure_bucket"] == "execution_schema_missing"
    assert "no such table" in str(summary["cases"][0]["execution_error"]).lower()


def test_llm_resolution_render_cli_accepts_utf8_bom_json(tmp_path: Path) -> None:
    packet = {
        "schema_version": 1,
        "source": "semsql_rejected_query_packet",
        "question": "show mail subjects",
        "route_reason": "manual_rejected",
        "schema_card": {
            "schema_version": 1,
            "source": "semsql_schema_card",
            "graph": "",
            "summary": {},
            "entities": [
                {
                    "name": "mails",
                    "db_table": "mails",
                    "fields": [
                        {
                            "name": "subject",
                            "db_column": "subject",
                            "type": "text",
                        }
                    ],
                }
            ],
            "relationships": [],
        },
        "local_candidates": {},
        "query_frame": None,
    }
    proposal = {
        "schema_version": 1,
        "action": "route",
        "confidence": 0.9,
        "intent": "list mail subjects",
        "target_entities": ["mails"],
        "projections": [
            {
                "kind": "field",
                "field": "mails.subject",
                "aggregate": "",
                "rationale": "subject is the requested display field",
            }
        ],
        "filters": [],
        "joins": [],
        "group_by": [],
        "order_by": [],
        "limit": 10,
        "ambiguity_questions": [],
        "evidence": [],
        "safety_notes": [],
    }
    packet_json = tmp_path / "packet.json"
    proposal_json = tmp_path / "proposal.json"
    packet_json.write_text(json.dumps(packet), encoding="utf-8-sig")
    proposal_json.write_text(json.dumps(proposal), encoding="utf-8-sig")

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "semsql_eval",
            "llm-resolution-render",
            "--packet-json",
            str(packet_json),
            "--proposal-json",
            str(proposal_json),
            "--strict",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_subprocess_env(),
    )

    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["valid"] is True
    assert payload["sql"] == 'SELECT "mails"."subject" FROM "mails" LIMIT 10'


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
    assert "relationships" in payload["coverage"]
    assert "sample_values" in payload["coverage"]
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
