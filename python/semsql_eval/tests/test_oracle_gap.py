from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner
from semsql_eval.__main__ import cli
from semsql_eval.oracle_gap import canonicalize_slot_value, oracle_gap_report


def _write_json(path: Path, examples: list[dict[str, object]]) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "summary": {
                    "suite": "bird",
                    "total": len(examples),
                    "correct": sum(bool(ex.get("exec_equal")) for ex in examples),
                    "exec_acc": 0.0,
                },
                "examples": examples,
            }
        ),
        encoding="utf-8",
    )


def _example(
    question: str,
    correct: bool,
    *,
    slots: list[dict[str, object]] | None = None,
    query_frame: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "db_id": "california_schools",
        "question": question,
        "gold_sql": "SELECT 1",
        "pred_sql": "SELECT 1" if correct else "SELECT 0",
        "exec_equal": correct,
        "stage3_slots": slots or [],
        "query_frame": query_frame,
    }


def test_oracle_gap_computes_recovery_and_partitions(tmp_path: Path) -> None:
    current = tmp_path / "current.json"
    oracle = tmp_path / "oracle.json"
    previous = tmp_path / "previous.json"
    cache = tmp_path / "cache.jsonl"

    current_examples = [
        _example("both correct", True),
        _example(
            "oracle only",
            False,
            slots=[
                {
                    "slot_name": "@field1",
                    "slot_kind": "field",
                    "picked": "frpm.low_grade",
                    "candidates": [{"value": "frpm.low_grade"}],
                },
                {
                    "slot_name": "@val1",
                    "slot_kind": "value",
                    "picked": "8",
                    "candidates": [{"value": "8"}],
                },
            ],
        ),
        _example("both wrong covered", False),
        _example("uncovered", False),
    ]
    oracle_examples = [
        _example("both correct", True),
        _example("oracle only", True),
        _example("both wrong covered", False),
        _example("uncovered", False),
    ]
    previous_examples = [
        _example("both correct", True),
        _example("oracle only", True),
        _example("both wrong covered", False),
        _example("uncovered", False),
    ]
    _write_json(current, current_examples)
    _write_json(oracle, oracle_examples)
    _write_json(previous, previous_examples)
    cache.write_text(
        "\n".join(
            [
                json.dumps({"db_id": "california_schools", "nl": "both correct", "slot_map": {}}),
                json.dumps(
                    {
                        "db_id": "california_schools",
                        "nl": "oracle only",
                        "slot_map": {"@field1": "frpm.low grade", "@val1": "9"},
                    }
                ),
                json.dumps({"db_id": "california_schools", "nl": "both wrong covered", "slot_map": {}}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    report = oracle_gap_report(
        current_report_json=current,
        oracle_report_json=oracle,
        oracle_cache_jsonl=cache,
        previous_report_json=previous,
    )

    assert report.oracle_cache_coverage == 0.75
    assert report.all_oracle_covered_correct == 2
    assert report.live_stage3_recovered == 1
    assert report.live_stage3_recovery == 0.5
    assert report.regressions_from_previous == 1
    assert report.partitions == {
        "both_correct": 1,
        "oracle_only": 1,
        "both_wrong_covered": 1,
        "current_only": 0,
        "uncovered": 1,
    }
    assert report.slot_mismatches_by_kind == {"value": 1}
    assert report.slot_gold_missing_by_kind == {"value": 1}
    assert report.slot_present_misranked_by_kind == {}
    assert report.oracle_only_triage["gold_absent_from_candidates"] == 1
    assert report.oracle_only_triage["present_but_misranked"] == 0


def test_oracle_gap_distinguishes_present_but_misranked_slots(tmp_path: Path) -> None:
    current = tmp_path / "current.json"
    oracle = tmp_path / "oracle.json"
    cache = tmp_path / "cache.jsonl"

    slots = [
        {
            "slot_name": "@val1",
            "slot_kind": "value",
            "picked": "8",
            "candidates": [{"value": "8"}, {"value": "9"}],
        }
    ]
    _write_json(current, [_example("oracle only", False, slots=slots)])
    _write_json(oracle, [_example("oracle only", True)])
    cache.write_text(
        json.dumps(
            {
                "db_id": "california_schools",
                "nl": "oracle only",
                "slot_map": {"@val1": "9"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    report = oracle_gap_report(
        current_report_json=current,
        oracle_report_json=oracle,
        oracle_cache_jsonl=cache,
    )

    assert report.slot_mismatches_by_kind == {"value": 1}
    assert report.slot_gold_missing_by_kind == {}
    assert report.slot_present_misranked_by_kind == {"value": 1}
    assert report.oracle_only_triage["present_but_misranked"] == 1
    assert report.examples[0].slot_gap_bucket == "present_but_misranked"


def test_oracle_gap_counts_gold_present_in_selected_bindings(tmp_path: Path) -> None:
    current = tmp_path / "current.json"
    oracle = tmp_path / "oracle.json"
    cache = tmp_path / "cache.jsonl"

    slots = [
        {
            "slot_name": "@val1",
            "slot_kind": "value",
            "picked": "'0613360'",
            "candidates": [{"value": "'0613360'"}, {"value": "50"}],
        }
    ]
    query_frame = {
        "stage3": {
            "selected_bindings": [
                {
                    "slot": "@val1",
                    "candidate_value": "50",
                    "mention_kind": "percentage",
                    "field": "frpm.percent_eligible",
                }
            ]
        }
    }
    _write_json(
        current,
        [_example("oracle only", False, slots=slots, query_frame=query_frame)],
    )
    _write_json(oracle, [_example("oracle only", True)])
    cache.write_text(
        json.dumps(
            {
                "db_id": "california_schools",
                "nl": "oracle only",
                "slot_map": {"@val1": "50"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    report = oracle_gap_report(
        current_report_json=current,
        oracle_report_json=oracle,
        oracle_cache_jsonl=cache,
    )

    assert report.slot_selected_binding_available_by_kind == {"value": 1}
    assert report.slot_selected_binding_missing_by_kind == {}
    assert report.examples[0].slot_selected_binding_available == 1
    assert report.examples[0].slot_selected_binding_missing == 0


def test_oracle_gap_counts_gold_missing_from_selected_bindings(tmp_path: Path) -> None:
    current = tmp_path / "current.json"
    oracle = tmp_path / "oracle.json"
    cache = tmp_path / "cache.jsonl"

    slots = [
        {
            "slot_name": "@val1",
            "slot_kind": "value",
            "picked": "'0613360'",
            "candidates": [{"value": "'0613360'"}, {"value": "50"}],
        }
    ]
    query_frame = {
        "stage3": {
            "selected_bindings": [
                {
                    "slot": "@val1",
                    "candidate_value": "'0613360'",
                    "mention_kind": "code",
                    "field": "schools.ncesdist",
                }
            ]
        }
    }
    _write_json(
        current,
        [_example("oracle only", False, slots=slots, query_frame=query_frame)],
    )
    _write_json(oracle, [_example("oracle only", True)])
    cache.write_text(
        json.dumps(
            {
                "db_id": "california_schools",
                "nl": "oracle only",
                "slot_map": {"@val1": "50"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    report = oracle_gap_report(
        current_report_json=current,
        oracle_report_json=oracle,
        oracle_cache_jsonl=cache,
    )

    assert report.slot_selected_binding_available_by_kind == {}
    assert report.slot_selected_binding_missing_by_kind == {"value": 1}
    assert report.examples[0].slot_selected_binding_available == 0
    assert report.examples[0].slot_selected_binding_missing == 1


def test_oracle_gap_splits_pre_stage3_absent_and_lost_candidates(tmp_path: Path) -> None:
    current = tmp_path / "current.json"
    oracle = tmp_path / "oracle.json"
    cache = tmp_path / "cache.jsonl"

    slots = [
        {
            "slot_name": "@field1",
            "slot_kind": "field",
            "picked": "frpm.low_grade",
            "candidates": [{"value": "frpm.low_grade"}],
        },
        {
            "slot_name": "@val1",
            "slot_kind": "value",
            "picked": "'Alameda'",
            "candidates": [{"value": "'Alameda'"}],
        },
    ]
    query_frame = {
        "pre_stage3": {
            "slots": [
                {
                    "slot": "@field1",
                    "candidates": [{"value": "frpm.county_name"}],
                },
                {
                    "slot": "@val1",
                    "candidates": [{"value": "'Alameda'"}, {"value": "'Lake'"}],
                },
            ]
        }
    }
    _write_json(
        current,
        [_example("oracle only", False, slots=slots, query_frame=query_frame)],
    )
    _write_json(oracle, [_example("oracle only", True)])
    cache.write_text(
        json.dumps(
            {
                "db_id": "california_schools",
                "nl": "oracle only",
                "slot_map": {"@field1": "frpm.county name", "@val1": "'Lake'"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    report = oracle_gap_report(
        current_report_json=current,
        oracle_report_json=oracle,
        oracle_cache_jsonl=cache,
    )

    assert report.slot_gold_missing_by_kind == {"field": 1, "value": 1}
    assert report.slot_pre_stage3_missing_by_kind == {}
    assert report.slot_lost_before_scoring_by_kind == {"field": 1, "value": 1}
    assert report.oracle_only_triage["gold_lost_before_scoring"] == 1
    assert report.examples[0].slot_gap_bucket == "gold_lost_before_scoring"


def test_oracle_gap_marks_gold_absent_before_stage3(tmp_path: Path) -> None:
    current = tmp_path / "current.json"
    oracle = tmp_path / "oracle.json"
    cache = tmp_path / "cache.jsonl"

    slots = [
        {
            "slot_name": "@field1",
            "slot_kind": "field",
            "picked": "frpm.low_grade",
            "candidates": [{"value": "frpm.low_grade"}],
        }
    ]
    query_frame = {
        "pre_stage3": {
            "slots": [
                {
                    "slot": "@field1",
                    "candidates": [{"value": "frpm.low_grade"}],
                }
            ]
        }
    }
    _write_json(
        current,
        [_example("oracle only", False, slots=slots, query_frame=query_frame)],
    )
    _write_json(oracle, [_example("oracle only", True)])
    cache.write_text(
        json.dumps(
            {
                "db_id": "california_schools",
                "nl": "oracle only",
                "slot_map": {"@field1": "frpm.county name"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    report = oracle_gap_report(
        current_report_json=current,
        oracle_report_json=oracle,
        oracle_cache_jsonl=cache,
    )

    assert report.slot_pre_stage3_missing_by_kind == {"field": 1}
    assert report.slot_lost_before_scoring_by_kind == {}
    assert report.oracle_only_triage["gold_absent_from_pre_stage3"] == 1
    assert report.examples[0].slot_gap_bucket == "gold_absent_from_pre_stage3"


def test_canonicalize_slot_value_treats_display_and_runtime_fields_equal() -> None:
    assert canonicalize_slot_value("frpm.low grade", "@field1") == "frpm.low_grade"
    assert canonicalize_slot_value("frpm.`Low Grade`", "@field1") == "frpm.low_grade"


def test_oracle_gap_cli_writes_markdown_and_json(tmp_path: Path) -> None:
    current = tmp_path / "current.json"
    oracle = tmp_path / "oracle.json"
    cache = tmp_path / "cache.jsonl"
    out = tmp_path / "gap.md"
    out_json = tmp_path / "gap.json"

    _write_json(current, [_example("q", True)])
    _write_json(oracle, [_example("q", True)])
    cache.write_text(
        json.dumps({"db_id": "california_schools", "nl": "q", "slot_map": {}}) + "\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli,
        [
            "oracle-gap",
            "--current-report-json",
            str(current),
            "--oracle-report-json",
            str(oracle),
            "--oracle-cache",
            str(cache),
            "--out",
            str(out),
            "--out-json",
            str(out_json),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "SemanticSQL Oracle Gap Report" in result.output
    assert out.exists()
    data = json.loads(out_json.read_text(encoding="utf-8"))
    assert data["live_stage3_recovery"] == 1.0
    assert data["provenance"]["generated_at_utc"].endswith("Z")
    assert data["provenance"]["semsql_eval_version"] == "0.1.0.dev0"
