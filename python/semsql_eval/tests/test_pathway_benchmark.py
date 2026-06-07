from __future__ import annotations

from semsql_eval.pathway_benchmark import (
    _suite_with_route_paraphrases,
    _variant_coverage,
    pathway_product_gate_failures,
    render_pathway_benchmark_markdown,
)


def test_render_pathway_benchmark_markdown_shows_policy_tradeoffs() -> None:
    report = {
        "benchmark": "pathway-decision-v1",
        "semsql_bin": "target/debug/semsql.exe",
        "paraphrase_variants_per_route": 0,
        "summary": {
            "cases_total": 3,
            "route_total": 2,
            "nonroute_total": 1,
            "policies": {
                "current_permissive": {
                    "route_correct": 1,
                    "route_wrong_sql": 1,
                    "route_fail_closed": 0,
                    "nonroute_fail_closed": 0,
                    "nonroute_unexpected_sql": 1,
                },
                "frame_only": {
                    "route_correct": 1,
                    "route_wrong_sql": 0,
                    "route_fail_closed": 1,
                    "nonroute_fail_closed": 1,
                    "nonroute_unexpected_sql": 0,
                },
                "bounded_stage3": {
                    "route_correct": 1,
                    "route_wrong_sql": 1,
                    "route_fail_closed": 0,
                    "nonroute_fail_closed": 1,
                    "nonroute_unexpected_sql": 0,
                },
            },
            "signals": {
                "stage3_sql_total": 1,
                "stage3_escalated_slots": 0,
                "runtime_routed": 1,
                "runtime_used_for_final_sql": 1,
                "frame_promoted": 1,
            },
            "current_wrong_by_family": {"topk_group_count": 1},
            "current_unexpected_by_family": {"unsafe_action": 1},
            "frame_only_false_negative_by_family": {"date_range_count": 1},
        },
        "cases": [
            {
                "suite": "platform",
                "id": "pq001",
                "disposition": "route",
                "family": "multi_join_filter_projection",
                "stage_pinned": "stage_0a",
                "policies": {
                    "current_permissive": {"bucket": "correct"},
                    "frame_only": {"bucket": "correct"},
                    "bounded_stage3": {"bucket": "correct"},
                },
            }
        ],
    }

    rendered = render_pathway_benchmark_markdown(report)

    assert "Pathway Decision Benchmark" in rendered
    assert "`current_permissive`" in rendered
    assert "`frame_only`" in rendered
    assert "`bounded_stage3`" in rendered
    assert "`topk_group_count`: `1`" in rendered
    assert "| `platform` | `pq001` | `route`" in rendered
    assert "paraphrase variants per route: `0`" in rendered


def test_route_paraphrases_keep_gold_sql_and_variant_metadata() -> None:
    original_question = "Which active accounts have no login events after March 1 2024?"
    suite = {
        "cases": [
            {
                "id": "pq007",
                "question": original_question,
                "disposition": "route",
                "family": "anti_join_temporal",
                "difficulty": "hard",
                "expected_sql": "SELECT 1",
            },
            {
                "id": "pq014",
                "question": "Email all accounts with overdue invoices",
                "disposition": "reject",
                "family": "unsafe_action",
                "difficulty": "easy",
                "expected_sql": None,
            },
        ]
    }

    expanded = _suite_with_route_paraphrases(suite, variants_per_route=1, seed=7)

    cases = expanded["cases"]
    assert len(cases) == 3
    variant = cases[1]
    assert variant["id"] == "pq007-p1"
    assert variant["variant_of"] == "pq007"
    assert variant["variant_kind"] == "seeded_paraphrase"
    assert variant["expected_sql"] == "SELECT 1"
    assert variant["question"] != original_question
    assert cases[2]["id"] == "pq014"


def test_variant_coverage_reports_under_generated_routes() -> None:
    coverage = _variant_coverage(
        [
            {
                "suite": "platform",
                "id": "pq001",
                "disposition": "route",
                "family": "multi_join_filter_projection",
                "variant_kind": "base",
            },
            {
                "suite": "platform",
                "id": "pq001-p1",
                "disposition": "route",
                "family": "multi_join_filter_projection",
                "variant_kind": "seeded_paraphrase",
                "variant_of": "pq001",
            },
            {
                "suite": "platform",
                "id": "pq014",
                "disposition": "reject",
                "family": "unsafe_action",
                "variant_kind": "base",
            },
        ],
        variants_per_route=2,
    )

    assert coverage["base_route_cases"] == 1
    assert coverage["requested_route_variants"] == 2
    assert coverage["generated_route_variants"] == 1
    assert coverage["route_cases_below_requested_variant_count"] == 1
    assert coverage["missing_by_case"] == [
        {
            "suite": "platform",
            "id": "pq001",
            "family": "multi_join_filter_projection",
            "requested": 2,
            "generated": 1,
        }
    ]


def test_pathway_product_gate_allows_fail_closed_route_gaps() -> None:
    report = _gate_report(
        route_total=4,
        nonroute_total=2,
        frame_route_wrong=0,
        bound_route_wrong=0,
        bound_nonroute_unexpected=0,
        signals={
            "frame_promoted_route_wrong_sql": 0,
            "stage3_route_wrong_sql": 0,
            "stage3_nonroute_unexpected_sql": 0,
            "stage3_sql_with_escalation": 0,
            "bound_plan_route_wrong_sql": 0,
            "bound_plan_nonroute_unexpected_sql": 0,
        },
    )

    assert pathway_product_gate_failures(report) == []


def test_pathway_product_gate_blocks_wrong_sql_and_missing_coverage() -> None:
    report = _gate_report(
        route_total=0,
        nonroute_total=0,
        frame_route_wrong=1,
        bound_route_wrong=2,
        bound_nonroute_unexpected=1,
        signals={
            "frame_promoted_route_wrong_sql": 1,
            "stage3_route_wrong_sql": 1,
            "stage3_nonroute_unexpected_sql": 1,
            "stage3_sql_with_escalation": 1,
            "bound_plan_route_wrong_sql": 2,
            "bound_plan_nonroute_unexpected_sql": 1,
        },
    )

    assert pathway_product_gate_failures(report) == [
        "no_route_cases",
        "no_nonroute_cases",
        "frame_only_route_wrong_sql",
        "bound_plan_route_wrong_sql",
        "bound_plan_nonroute_unexpected_sql",
        "frame_promoted_route_wrong_sql",
        "stage3_route_wrong_sql",
        "stage3_nonroute_unexpected_sql",
        "stage3_sql_with_escalation",
    ]


def _gate_report(
    *,
    route_total: int,
    nonroute_total: int,
    frame_route_wrong: int,
    bound_route_wrong: int,
    bound_nonroute_unexpected: int,
    signals: dict[str, int] | None = None,
) -> dict[str, object]:
    return {
        "summary": {
            "route_total": route_total,
            "nonroute_total": nonroute_total,
            "policies": {
                "frame_only": {
                    "route_wrong_sql": frame_route_wrong,
                    "nonroute_unexpected_sql": 0,
                },
                "bound_plan": {
                    "route_wrong_sql": bound_route_wrong,
                    "nonroute_unexpected_sql": bound_nonroute_unexpected,
                },
            },
            "signals": signals or {},
        }
    }
