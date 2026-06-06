from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner
from semsql_train.__main__ import cli
from semsql_train.runtime_slot_pairs import (
    derive_runtime_slot_pairs,
    load_oracle_slot_maps,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True))
            fh.write("\n")


def test_load_oracle_slot_maps_canonicalizes_display_fields(tmp_path: Path) -> None:
    cache = tmp_path / "oracle.jsonl"
    _write_jsonl(
        cache,
        [
            {
                "db_id": "california_schools",
                "nl": "charter schools in Fresno",
                "slot_map": {
                    "@field1": "frpm.Charter School (Y/N)",
                    "@entity1": "FRPM",
                    "@val1": "'Fresno'",
                },
            }
        ],
    )

    maps = load_oracle_slot_maps(cache)

    assert maps[("california_schools", "charter schools in Fresno")] == {
        "@field1": "frpm.charter_school_y_n",
        "@entity1": "frpm",
        "@val1": "'Fresno'",
    }


def test_derive_runtime_slot_pairs_uses_runtime_context_and_hard_negatives(
    tmp_path: Path,
) -> None:
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "examples": [
                    {
                        "db_id": "demo",
                        "question": "active orders",
                        "stage3_slots": [
                            {
                                "slot_name": "@val1",
                                "slot_kind": "value",
                                "context_skeleton": (
                                    "SELECT @field1 FROM @entity1 WHERE @field2 = @val1"
                                ),
                                "predicate_field": "orders.status",
                                "candidates": [
                                    {"value": "'pending'"},
                                    {"value": "'closed'"},
                                    {"value": "'active'"},
                                    {"value": "'cancelled'"},
                                ],
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    rows, stats = derive_runtime_slot_pairs(
        report,
        {("demo", "active orders"): {"@val1": "'active'"}},
        max_candidates=3,
    )

    assert stats.pairs_written == 1
    assert rows == [
        {
            "stage": 3,
            "nl": "active orders",
            "skeleton": "SELECT @field1 FROM @entity1 WHERE @field2 = @val1",
            "slot_name": "@val1",
            "candidates": ["'pending'", "'closed'", "'active'"],
            "correct_index": 2,
            "runtime_trace": True,
            "runtime_context_mode": "actual",
            "slot_kind": "value",
            "predicate_field": "orders.status",
        }
    ]


def test_derive_runtime_slot_pairs_can_append_missing_gold(tmp_path: Path) -> None:
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "examples": [
                    {
                        "db_id": "demo",
                        "question": "active orders",
                        "stage3_slots": [
                            {
                                "slot_name": "@val1",
                                "context_skeleton": "SELECT * FROM @entity1 WHERE @field1 = @val1",
                                "candidates": [{"value": "'pending'"}],
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    rows, stats = derive_runtime_slot_pairs(
        report,
        {("demo", "active orders"): {"@val1": "'active'"}},
    )

    assert stats.gold_appended == 1
    assert rows[0]["candidates"] == ["'pending'", "'active'"]
    assert rows[0]["correct_index"] == 1
    assert rows[0]["runtime_context_mode"] == "actual"


def test_derive_runtime_slot_pairs_can_teacher_force_previous_slots(
    tmp_path: Path,
) -> None:
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "examples": [
                    {
                        "db_id": "demo",
                        "question": "active users",
                        "stage3_slots": [
                            {
                                "slot_name": "@entity1",
                                "context_skeleton": (
                                    "SELECT @field1 FROM @entity1 WHERE @field2 = @val1"
                                ),
                                "candidates": [{"value": "orders"}, {"value": "users"}],
                            },
                            {
                                "slot_name": "@field1",
                                "context_skeleton": (
                                    "SELECT @field1 FROM orders WHERE @field2 = @val1"
                                ),
                                "candidates": [{"value": "orders.id"}, {"value": "users.id"}],
                            },
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    rows, stats = derive_runtime_slot_pairs(
        report,
        {
            ("demo", "active users"): {
                "@entity1": "users",
                "@field1": "users.id",
            }
        },
        context_mode="teacher-forced",
    )

    assert stats.pairs_written == 2
    assert rows[0]["skeleton"] == "SELECT @field1 FROM @entity1 WHERE @field2 = @val1"
    assert rows[1]["skeleton"] == "SELECT @field1 FROM users WHERE @field2 = @val1"
    assert rows[1]["runtime_context_mode"] == "teacher_forced"


def test_derive_runtime_slot_pairs_cli_writes_jsonl(tmp_path: Path) -> None:
    cache = tmp_path / "oracle.jsonl"
    _write_jsonl(
        cache,
        [
            {
                "db_id": "demo",
                "nl": "active orders",
                "slot_map": {"@val1": "'active'"},
            }
        ],
    )
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "examples": [
                    {
                        "db_id": "demo",
                        "question": "active orders",
                        "stage3_slots": [
                            {
                                "slot_name": "@val1",
                                "context_skeleton": "SELECT * FROM @entity1 WHERE @field1 = @val1",
                                "candidates": [{"value": "'active'"}, {"value": "'pending'"}],
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    out = tmp_path / "runtime-slot.jsonl"

    result = CliRunner().invoke(
        cli,
        [
            "derive-runtime-slot-pairs",
            "--report-json",
            str(report),
            "--oracle-cache",
            str(cache),
            "--out",
            str(out),
            "--context-mode",
            "teacher-forced",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "wrote 1 runtime Stage 3 pairs" in result.output
    row = json.loads(out.read_text(encoding="utf-8"))
    assert row["runtime_trace"] is True
    assert row["runtime_context_mode"] == "teacher_forced"
