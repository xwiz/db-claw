from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner
from semsql_eval.__main__ import cli
from semsql_eval.ablation_gap import (
    ablation_gap_report,
    render_ablation_gap_markdown,
)


def _write_report(path: Path, rows: list[tuple[str, bool]]) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "summary": {
                    "suite": "bird",
                    "total": len(rows),
                    "correct": sum(1 for _, correct in rows if correct),
                    "exec_acc": sum(1 for _, correct in rows if correct) / len(rows),
                },
                "examples": [
                    {
                        "db_id": "db",
                        "question": question,
                        "gold_sql": f"SELECT '{question}'",
                        "pred_sql": f"SELECT '{question}:{correct}'",
                        "exec_equal": correct,
                    }
                    for question, correct in rows
                ],
            }
        ),
        encoding="utf-8",
    )


def test_ablation_gap_partitions_live_path_recovery(tmp_path: Path) -> None:
    live = tmp_path / "live.json"
    schema = tmp_path / "schema.json"
    stage2 = tmp_path / "stage2.json"
    both = tmp_path / "both.json"
    all_oracle = tmp_path / "all.json"
    questions = [
        "live",
        "schema only",
        "stage2 only",
        "coupled",
        "stage3",
        "ceiling",
    ]
    _write_report(live, [(q, q == "live") for q in questions])
    _write_report(schema, [(q, q == "schema only") for q in questions])
    _write_report(stage2, [(q, q == "stage2 only") for q in questions])
    _write_report(
        both,
        [(q, q in {"schema only", "stage2 only", "coupled"}) for q in questions],
    )
    _write_report(
        all_oracle,
        [(q, q in {"schema only", "stage2 only", "coupled", "stage3"}) for q in questions]
        + [("extra larger report row", True)],
    )

    report = ablation_gap_report(
        live_report_json=live,
        oracle_schema_report_json=schema,
        oracle_stage2_report_json=stage2,
        oracle_schema_stage2_report_json=both,
        all_oracle_report_json=all_oracle,
    )

    assert report.accuracies["live"]["correct"] == 1
    assert report.accuracies["all_oracle"]["total"] == 6
    assert report.accuracies["all_oracle"]["correct"] == 4
    assert report.partitions["live_correct"] == 1
    assert report.partitions["schema_linking_blocked"] == 1
    assert report.partitions["stage2_shape_blocked"] == 1
    assert report.partitions["schema_and_stage2_coupled"] == 1
    assert report.partitions["stage3_or_renderer_blocked"] == 1
    assert report.partitions["oracle_ceiling_or_dataset"] == 1


def test_render_ablation_gap_markdown(tmp_path: Path) -> None:
    live = tmp_path / "live.json"
    both = tmp_path / "both.json"
    _write_report(live, [("q", False)])
    _write_report(both, [("q", True)])

    rendered = render_ablation_gap_markdown(
        ablation_gap_report(
            live_report_json=live,
            oracle_schema_stage2_report_json=both,
        )
    )

    assert "SemanticSQL Live-Path Ablation Gap" in rendered
    assert "`schema_and_stage2_coupled`: 1" in rendered
    assert "## Accuracy Ladder" in rendered


def test_ablation_gap_cli_writes_markdown_and_json(tmp_path: Path) -> None:
    live = tmp_path / "live.json"
    stage2 = tmp_path / "stage2.json"
    markdown = tmp_path / "gap.md"
    out_json = tmp_path / "gap.json"
    _write_report(live, [("q", False)])
    _write_report(stage2, [("q", True)])

    result = CliRunner().invoke(
        cli,
        [
            "ablation-gap",
            "--live-report-json",
            str(live),
            "--oracle-stage2-report-json",
            str(stage2),
            "--out",
            str(markdown),
            "--out-json",
            str(out_json),
        ],
    )

    assert result.exit_code == 0, result.output
    assert markdown.exists()
    data = json.loads(out_json.read_text(encoding="utf-8"))
    assert data["partitions"]["stage2_shape_blocked"] == 1
