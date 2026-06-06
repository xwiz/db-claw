from __future__ import annotations

from semsql_eval.real_app_framework_probe import (
    _canonical_is_grounded,
    _metric_probe_question,
    _metric_sql_matches,
    _summarize,
)


def test_scope_predicate_json_grounding_uses_typed_field() -> None:
    fields = {"org_plugins.is_active", "org_plugins.status"}
    entities = {"org_plugins"}

    assert _canonical_is_grounded(
        "scope_predicate",
        '{"field":"org_plugins.is_active","operator":"=","rawValue":"1"}',
        entities,
        fields,
    )
    assert not _canonical_is_grounded(
        "scope_predicate",
        '{"field":"missing.is_active","operator":"=","rawValue":"1"}',
        entities,
        fields,
    )


def test_real_app_probe_summary_requires_grounded_source_vocab() -> None:
    raw = {"returncode": 0, "record_count": 10}
    graph = {
        "entity_count": 2,
        "field_count": 4,
        "source_vocab_count": 3,
        "source_vocab_grounded": 2,
        "source_vocab_dangling": [{}],
        "sample_value_count": 0,
        "metric_definition_count": 0,
    }

    summary = _summarize(
        raw=raw,
        graph=graph,
        query_checks=[],
        metric_packet_checks=[],
        min_source_vocab=1,
        min_query_checks=0,
        min_metric_packet_checks=0,
        sample_values=False,
    )

    assert not summary["pass"]
    assert summary["source_vocab_dangling"] == 1


def test_real_app_probe_summary_requires_requested_query_checks() -> None:
    raw = {"returncode": 0, "record_count": 10}
    graph = {
        "entity_count": 2,
        "field_count": 4,
        "source_vocab_count": 3,
        "source_vocab_grounded": 3,
        "source_vocab_dangling": [],
        "sample_value_count": 0,
        "metric_definition_count": 0,
    }

    summary = _summarize(
        raw=raw,
        graph=graph,
        query_checks=[],
        metric_packet_checks=[],
        min_source_vocab=1,
        min_query_checks=1,
        min_metric_packet_checks=0,
        sample_values=False,
    )

    assert not summary["pass"]
    assert not summary["query_check_count_ok"]


def test_real_app_probe_summary_requires_requested_metric_packet_checks() -> None:
    raw = {"returncode": 0, "record_count": 10}
    graph = {
        "entity_count": 2,
        "field_count": 4,
        "source_vocab_count": 3,
        "source_vocab_grounded": 3,
        "source_vocab_dangling": [],
        "sample_value_count": 0,
        "metric_definition_count": 1,
    }

    summary = _summarize(
        raw=raw,
        graph=graph,
        query_checks=[],
        metric_packet_checks=[],
        min_source_vocab=1,
        min_query_checks=0,
        min_metric_packet_checks=1,
        sample_values=False,
    )

    assert not summary["pass"]
    assert not summary["metric_packet_check_count_ok"]


def test_metric_probe_question_prefers_authored_alias() -> None:
    assert (
        _metric_probe_question(
            {
                "name": "active_user_rate",
                "display_label": "Active User Rate",
                "aliases": ["active customers"],
            }
        )
        == "active customers"
    )


def test_metric_sql_matches_aggregate_and_distinct_count() -> None:
    assert _metric_sql_matches(
        {
            "metric_kind": "aggregate",
            "aggregate": "AVG",
            "measure_field": "game_scores.score",
            "distinct": False,
        },
        'SELECT AVG("game_scores"."score") FROM "game_scores"',
    )
    assert _metric_sql_matches(
        {
            "metric_kind": "aggregate",
            "aggregate": "COUNT",
            "measure_field": "game_scores.user_id",
            "distinct": True,
        },
        'SELECT COUNT(DISTINCT "game_scores"."user_id") FROM "game_scores"',
    )
    assert not _metric_sql_matches(
        {
            "metric_kind": "aggregate",
            "aggregate": "COUNT",
            "measure_field": "game_scores.user_id",
            "distinct": True,
        },
        'SELECT COUNT("game_scores"."user_id") FROM "game_scores"',
    )


def test_metric_sql_matches_conditional_rate_fields() -> None:
    assert _metric_sql_matches(
        {
            "metric_kind": "conditional_rate",
            "numerator_field": "daily_anchors.is_completed",
            "denominator_field": "daily_anchors.id",
        },
        (
            'SELECT SUM(CASE WHEN "daily_anchors"."is_completed" = TRUE '
            'THEN 1 ELSE 0 END) / COUNT("daily_anchors"."id") FROM "daily_anchors"'
        ),
    )
