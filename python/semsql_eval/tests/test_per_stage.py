"""Per-stage eval metric tests."""

from __future__ import annotations

import math

import pytest

from semsql_eval.per_stage import (
    RankedItem,
    linker_ndcg_at_k,
    linker_recall_at_k,
    skeleton_exact_match,
    slot_top1_accuracy,
)


# ---------------------------------------------------------------------------
# Stage 1 (linker) — recall@k
# ---------------------------------------------------------------------------


def test_recall_perfect_ranking() -> None:
    items = [
        RankedItem("q1", "users", 0.99, 1.0),
        RankedItem("q1", "users.email", 0.97, 1.0),
        RankedItem("q1", "orders", 0.10, 0.0),
        RankedItem("q1", "products", 0.05, 0.0),
    ]
    out = linker_recall_at_k(items, k=5)
    assert out.recall_at_k == 1.0
    assert out.questions == 1
    assert math.isclose(out.ndcg_at_k, 1.0)


def test_recall_partial_when_one_relevant_falls_outside_topk() -> None:
    # 2 relevant items, k=2, but the second relevant ranks 3rd —
    # recall@2 = 1/2.
    items = [
        RankedItem("q1", "users", 0.99, 1.0),
        RankedItem("q1", "noise_a", 0.90, 0.0),
        RankedItem("q1", "users.email", 0.50, 1.0),
        RankedItem("q1", "noise_b", 0.10, 0.0),
    ]
    out = linker_recall_at_k(items, k=2)
    assert math.isclose(out.recall_at_k, 0.5)


def test_recall_skips_questions_with_no_relevant() -> None:
    items = [
        RankedItem("q_no_relevant", "x", 0.99, 0.0),
        RankedItem("q1", "users", 0.99, 1.0),
    ]
    out = linker_recall_at_k(items, k=5)
    assert out.questions == 1   # only q1 counted
    assert out.recall_at_k == 1.0


def test_recall_macro_averages_over_questions() -> None:
    # q1: perfect (1.0), q2: 0.5 → macro 0.75.
    items = [
        RankedItem("q1", "users", 0.99, 1.0),
        RankedItem("q1", "noise", 0.10, 0.0),
        # q2: 2 relevant, k=1 captures 1 of them → 0.5.
        RankedItem("q2", "a", 0.99, 1.0),
        RankedItem("q2", "b", 0.50, 1.0),
    ]
    out = linker_recall_at_k(items, k=1)
    assert math.isclose(out.recall_at_k, (1.0 + 0.5) / 2)


# ---------------------------------------------------------------------------
# Stage 1 — nDCG
# ---------------------------------------------------------------------------


def test_ndcg_rewards_correct_ordering() -> None:
    items_correct = [
        RankedItem("q", "a", 0.99, 1.0),
        RankedItem("q", "b", 0.10, 0.0),
    ]
    items_swapped = [
        RankedItem("q", "a", 0.10, 1.0),
        RankedItem("q", "b", 0.99, 0.0),
    ]
    assert linker_ndcg_at_k(items_correct, k=2) > linker_ndcg_at_k(items_swapped, k=2)


def test_ndcg_zero_when_no_relevant_items() -> None:
    items = [
        RankedItem("q", "x", 0.99, 0.0),
        RankedItem("q", "y", 0.50, 0.0),
    ]
    assert linker_ndcg_at_k(items, k=5) == 0.0


# ---------------------------------------------------------------------------
# Stage 2 — skeleton exact match
# ---------------------------------------------------------------------------


def test_skeleton_match_normalises_whitespace() -> None:
    pairs = [
        ("SELECT * FROM @entity1", "SELECT  *   FROM   @entity1"),
        ("SELECT @field1 FROM @entity1", "SELECT @field1 FROM @entity2"),
    ]
    assert math.isclose(skeleton_exact_match(pairs), 0.5)


# ---------------------------------------------------------------------------
# Stage 3 — slot top-1
# ---------------------------------------------------------------------------


def test_slot_top1_correct() -> None:
    triples = [
        ("@entity1", ["users", "orders"], 0),  # gold = 0, top = "users" ✓
        ("@field1", ["orders.id", "users.id"], 1),  # gold = 1, top = "orders.id" ✗
    ]
    assert math.isclose(slot_top1_accuracy(triples), 0.5)


def test_slot_top1_empty_candidates_count_as_miss() -> None:
    triples = [("@entity1", [], 0)]
    assert slot_top1_accuracy(triples) == 0.0
