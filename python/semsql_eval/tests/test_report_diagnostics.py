from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner
from semsql_eval.__main__ import cli
from semsql_eval.report_diagnostics import diagnose_report, render_diagnosis_markdown


def _write_report(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "summary": {
                    "suite": "bird",
                    "total": 3,
                    "correct": 0,
                    "wrong": 2,
                    "errored": 1,
                    "exec_acc": 0.0,
                },
                "examples": [
                    {
                        "db_id": "california_schools",
                        "question": "Please list phone numbers of direct funded schools.",
                        "gold_sql": (
                            "SELECT T2.Phone FROM frpm AS T1 "
                            "INNER JOIN schools AS T2 ON T1.CDSCode = T2.CDSCode "
                            "WHERE T1.`Charter Funding Type` = 'Directly funded' "
                            "AND T2.OpenDate > '2000-01-01'"
                        ),
                        "pred_sql": (
                            "SELECT COUNT(*) FROM frpm "
                            "WHERE frpm.`Charter Funding Type` = 1"
                        ),
                        "failure_bucket": "exec_mismatch",
                        "exec_equal": False,
                    },
                    {
                        "db_id": "california_schools",
                        "question": "What is the highest eligible free rate?",
                        "gold_sql": (
                            "SELECT `Free Meal Count (K-12)` / `Enrollment (K-12)` "
                            "FROM frpm WHERE `County Name` = 'Alameda' "
                            "ORDER BY CAST(`Free Meal Count (K-12)` AS REAL) / "
                            "`Enrollment (K-12)` DESC LIMIT 1"
                        ),
                        "pred_sql": "SELECT COUNT(*) FROM frpm",
                        "failure_bucket": "exec_mismatch",
                        "exec_equal": False,
                    },
                    {
                        "db_id": "financial",
                        "question": "bad SQL example",
                        "gold_sql": "SELECT * FROM account",
                        "pred_sql": "SELECT * FROM missing_table",
                        "failure_bucket": "pred_exec_error",
                        "exec_equal": False,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )


def test_diagnose_report_classifies_stage_lanes(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    _write_report(report_path)

    report = diagnose_report(report_path)

    assert report.suite == "bird"
    assert report.total == 3
    assert report.lane_counts["schema_linker_or_join_planning"] >= 1
    assert report.lane_counts["skeleton_planning"] >= 1
    assert report.lane_counts["slot_value_grounding"] >= 1
    assert report.lane_counts["runtime_contract"] == 1
    assert report.tag_counts["missing_join"] == 1
    assert report.tag_counts["ratio_or_metric_collapsed_to_count"] == 1
    assert report.gold_feature_counts["join"] == 1
    assert report.pred_feature_counts["join"] == 0
    assert report.by_db["california_schools"]["wrong"] == 2
    assert report.by_db["financial"]["failure_buckets"]["pred_exec_error"] == 1


def test_render_diagnosis_markdown_surfaces_recommendations(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    _write_report(report_path)

    rendered = render_diagnosis_markdown(diagnose_report(report_path))

    assert "## Fix Lanes" in rendered
    assert "## By DB" in rendered
    assert "`california_schools`" in rendered
    assert "`skeleton_planning`" in rendered
    assert "`missing_join`" in rendered
    assert "Recommended Next Moves" in rendered
    assert "Stage 2 skeleton" in rendered


def test_diagnose_report_cli_writes_markdown_and_json(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    markdown_path = tmp_path / "diagnosis.md"
    json_path = tmp_path / "diagnosis.json"
    _write_report(report_path)

    result = CliRunner().invoke(
        cli,
        [
            "diagnose-report",
            "--report-json",
            str(report_path),
            "--out",
            str(markdown_path),
            "--out-json",
            str(json_path),
            "--sample-examples",
            "2",
        ],
    )

    assert result.exit_code == 0, result.output
    assert markdown_path.exists()
    assert json_path.exists()
    assert "SemanticSQL Report Diagnosis" in result.output
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["lane_counts"]["runtime_contract"] == 1
    assert data["by_db"]["california_schools"]["wrong"] == 2
    assert len(data["examples"]) == 2
