from __future__ import annotations

import math

from semsql_train.stage_eval import _linker_metrics, _slot_metrics


def test_linker_metrics_macro_recall_and_ndcg() -> None:
    rows = [
        {
            "nl": "q1",
            "candidate_target": "users",
            "relevance_label": 1.0,
            "score": 0.9,
        },
        {
            "nl": "q1",
            "candidate_target": "orders",
            "relevance_label": 0.0,
            "score": 0.2,
        },
        {
            "nl": "q2",
            "candidate_target": "a",
            "relevance_label": 1.0,
            "score": 0.8,
        },
        {
            "nl": "q2",
            "candidate_target": "b",
            "relevance_label": 1.0,
            "score": 0.1,
        },
    ]

    metrics, failures = _linker_metrics(rows, k=1, sample_examples=5)

    assert math.isclose(metrics["recall_at_k"], 0.75)
    assert metrics["questions"] == 2.0
    assert failures and failures[0]["missing"] == ["b"]


def test_linker_metrics_reports_runtime_shaped_recall() -> None:
    rows = [
        {
            "nl": "q",
            "candidate_kind": "field",
            "candidate_target": f"orders.f{i}",
            "relevance_label": 0.0,
            "score": 1.0 - (i * 0.01),
        }
        for i in range(5)
    ]
    rows.extend(
        [
            {
                "nl": "q",
                "candidate_kind": "entity",
                "candidate_target": "orders",
                "relevance_label": 1.0,
                "score": 0.5,
            },
            {
                "nl": "q",
                "candidate_kind": "field",
                "candidate_target": "orders.user_id",
                "relevance_label": 1.0,
                "score": 0.4,
            },
        ]
    )

    metrics, _failures = _linker_metrics(rows, k=5, sample_examples=5)

    assert metrics["recall_at_k"] == 0.0
    assert metrics["runtime_recall_at_entities3_fields7"] == 1.0
    assert metrics["entity_recall_at_3"] == 1.0
    assert metrics["field_recall_at_7"] == 1.0


def test_slot_metrics_breaks_out_slot_kinds() -> None:
    predictions = [
        {
            "slot_name": "@entity1",
            "correct_index": 0,
            "pred_index": 0,
        },
        {
            "slot_name": "@field1",
            "correct_index": 1,
            "pred_index": 0,
        },
        {
            "slot_name": "@val1",
            "correct_index": 0,
            "pred_index": 0,
        },
    ]

    metrics, failures = _slot_metrics(predictions, sample_examples=5)

    assert math.isclose(metrics["top1_accuracy"], 2 / 3)
    assert metrics["entity_top1_accuracy"] == 1.0
    assert metrics["field_top1_accuracy"] == 0.0
    assert metrics["value_top1_accuracy"] == 1.0
    assert len(failures) == 1
