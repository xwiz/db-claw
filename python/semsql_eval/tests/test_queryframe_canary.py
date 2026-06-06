"""Tests for the QueryFrame canary report renderer."""

from __future__ import annotations

from pathlib import Path

from semsql_eval.queryframe_canary import (
    render_queryframe_canary_markdown,
    render_queryframe_canary_suite_markdown,
    render_queryframe_mysql_canary_markdown,
    render_queryframe_postgres_canary_markdown,
    run_queryframe_mysql_canary,
    run_queryframe_postgres_canary,
)


def test_render_queryframe_canary_markdown_shows_stoplight() -> None:
    report = {
        "seed": 7,
        "variant": "alias",
        "corpus_dir": "target/queryframe_canary",
        "summary": {
            "pass": False,
            "routed_total": 2,
            "routed_correct": 1,
            "reject_total": 1,
            "reject_fail_closed": 1,
        },
        "routed_cases": [
            {"bucket": "correct"},
            {"bucket": "exec_mismatch"},
        ],
        "reject_cases": [
            {"bucket": "rejected"},
        ],
    }

    rendered = render_queryframe_canary_markdown(report)

    assert "status: `FAIL`" in rendered
    assert "variant: `alias`" in rendered
    assert "routed exec accuracy: `1/2`" in rendered
    assert "`exec_mismatch`: `1`" in rendered
    assert "`rejected`: `1`" in rendered


def test_render_queryframe_canary_suite_markdown_shows_matrix() -> None:
    report = {
        "seeds": [1, 2],
        "variants": ["commerce", "random_alias"],
        "summary": {
            "pass": True,
            "run_total": 2,
            "run_passed": 2,
            "routed_total": 32,
            "routed_correct": 32,
            "reject_total": 4,
            "reject_fail_closed": 4,
        },
        "runs": [
            {
                "variant": "commerce",
                "seed": 1,
                "summary": {
                    "pass": True,
                    "routed_total": 16,
                    "routed_correct": 16,
                    "reject_total": 2,
                    "reject_fail_closed": 2,
                },
            },
            {
                "variant": "random_alias",
                "seed": 2,
                "summary": {
                    "pass": True,
                    "routed_total": 16,
                    "routed_correct": 16,
                    "reject_total": 2,
                    "reject_fail_closed": 2,
                },
            },
        ],
    }

    rendered = render_queryframe_canary_suite_markdown(report)

    assert "status: `PASS`" in rendered
    assert "runs: `2/2`" in rendered
    assert "| `random_alias` | `2` | `16/16` | `2/2` | `PASS` |" in rendered


def test_render_queryframe_postgres_canary_markdown_shows_skip() -> None:
    report = {
        "status": "skipped",
        "seed": 7,
        "variant": "alias",
        "schema": "semsql_qf_alias_7",
        "corpus_dir": "target/queryframe_canary_postgres",
        "setup_sql": "target/queryframe_canary_postgres/postgres/setup.sql",
        "skip_reason": "missing_db_url",
        "skip_detail": "pass --db-url",
        "summary": {"skipped": True},
    }

    rendered = render_queryframe_postgres_canary_markdown(report)

    assert "status: `SKIPPED`" in rendered
    assert "reason: `missing_db_url`" in rendered


def test_run_queryframe_postgres_canary_skips_without_url(tmp_path: Path) -> None:
    report = run_queryframe_postgres_canary(
        out_dir=tmp_path / "pg_canary",
        seed=7,
        variant="alias",
        semsql_bin=tmp_path / "missing-semsql",
        db_url=None,
    )

    assert report["status"] == "skipped"
    assert report["skip_reason"] == "missing_db_url"
    assert report["summary"]["pass"] is False
    assert (tmp_path / "pg_canary" / "postgres" / "setup.sql").exists()


def test_render_queryframe_mysql_canary_markdown_shows_skip() -> None:
    report = {
        "status": "skipped",
        "seed": 7,
        "variant": "alias",
        "database": "semsql_qf_alias_7",
        "corpus_dir": "target/queryframe_canary_mysql",
        "setup_sql": "target/queryframe_canary_mysql/mysql/setup.sql",
        "skip_reason": "missing_db_url",
        "skip_detail": "pass --db-url",
        "summary": {"skipped": True},
    }

    rendered = render_queryframe_mysql_canary_markdown(report)

    assert "status: `SKIPPED`" in rendered
    assert "database: `semsql_qf_alias_7`" in rendered
    assert "reason: `missing_db_url`" in rendered


def test_run_queryframe_mysql_canary_skips_without_url(tmp_path: Path) -> None:
    report = run_queryframe_mysql_canary(
        out_dir=tmp_path / "mysql_canary",
        seed=7,
        variant="alias",
        semsql_bin=tmp_path / "missing-semsql",
        db_url=None,
    )

    assert report["status"] == "skipped"
    assert report["skip_reason"] == "missing_db_url"
    assert report["summary"]["pass"] is False
    assert (tmp_path / "mysql_canary" / "mysql" / "setup.sql").exists()
