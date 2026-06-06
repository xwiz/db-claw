from __future__ import annotations

from pathlib import Path

import pytest
import semsql_eval.realdb_schema_probe as realdb_schema_probe
from semsql_eval.realdb_schema_probe import (
    RealDbColumn,
    RealDbRelationship,
    RealDbTable,
    _ambiguous_physical_family_tables,
    _classify_governed_sql,
    _postgres_url_with_database,
    _run_probe_question,
    _select_analytics_questions,
    _select_count_tables,
    _shape_matches_question,
    _summarize_suite_runs,
    humanize_identifier,
    name_looks_sensitive,
    redact_db_url,
    render_mysql_realdb_schema_probe_markdown,
    render_mysql_realdb_schema_probe_suite_markdown,
    render_postgres_realdb_schema_probe_markdown,
    render_postgres_realdb_schema_probe_suite_markdown,
    run_mysql_realdb_schema_probe,
    run_postgres_realdb_schema_probe,
    select_typed_fallback_filtered_grouped_metric_questions,
    select_typed_fallback_grouped_metric_questions,
    select_typed_fallback_joined_filtered_grouped_metric_questions,
    select_typed_fallback_multi_joined_filtered_grouped_metric_questions,
    select_typed_fallback_multi_series_metric_questions,
    select_typed_fallback_rate_questions,
    select_typed_fallback_value_filtered_grouped_metric_questions,
)


def test_render_mysql_realdb_schema_probe_markdown_shows_stoplight() -> None:
    report = {
        "status": "pass",
        "database": "app",
        "graph": "target/app.schemaonly.semsql",
        "high_risk_schema": True,
        "safety_mode": "schema-only extraction; count-only execution",
        "summary": {
            "questions": 2,
            "routed": 1,
            "required_questions": 2,
            "required_ok": 2,
            "count_only_routes": 1,
            "executed_count_only_queries": 1,
            "analytics_questions": 0,
            "analytics_ok": 0,
            "executed_governed_analytics_queries": 0,
            "execution_errors": 0,
            "safe_not_executed": 1,
            "semantic_ok_or_safe_not_executed": 2,
            "needs_review": 0,
            "sample_value_rows": 0,
            "stages": {"stage_0a": 1, "needs_model": 1},
            "pass": True,
            "skipped": False,
        },
        "records": [
            {
                "index": 1,
                "question": "how many users",
                "stage": "stage_0a",
                "expected_table": "users",
                "expected_kind": "table_count",
                "expected_field": None,
                "actual_shape": "table_count",
                "actual_table": "users",
                "actual_field": None,
                "actual_count_table": "users",
                "count_only": True,
                "executed": True,
                "exec_status": "ok",
                "review": "ok",
                "sql": "SELECT COUNT(*) FROM `users`",
            },
            {
                "index": 2,
                "question": "list personal access tokens",
                "stage": "needs_model",
                "expected_table": None,
                "expected_kind": "table_count",
                "expected_field": None,
                "actual_shape": None,
                "actual_table": None,
                "actual_field": None,
                "actual_count_table": None,
                "count_only": False,
                "executed": False,
                "exec_status": "query_rejected_not_executed",
                "review": "expected_not_executed",
                "sql": "",
            },
        ],
    }

    rendered = render_mysql_realdb_schema_probe_markdown(report)

    assert "status: `PASS`" in rendered
    assert "database: `app`" in rendered
    assert "required contract: `2/2`" in rendered
    assert "semantic ok or safe not-executed: `2/2`" in rendered
    assert "`query_rejected_not_executed`" in rendered


def test_run_mysql_realdb_schema_probe_skips_without_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SEMSQL_MYSQL_PROBE_URL", raising=False)
    report = run_mysql_realdb_schema_probe(
        out_dir=tmp_path / "probe",
        semsql_bin=tmp_path / "missing-semsql",
        db_url=None,
    )

    assert report["status"] == "skipped"
    assert report["skip_reason"] == "missing_db_url"
    assert report["summary"]["pass"] is False


def test_run_postgres_realdb_schema_probe_skips_without_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SEMSQL_POSTGRES_PROBE_URL", raising=False)
    report = run_postgres_realdb_schema_probe(
        out_dir=tmp_path / "probe",
        semsql_bin=tmp_path / "missing-semsql",
        db_url=None,
    )

    assert report["engine"] == "postgres"
    assert report["status"] == "skipped"
    assert report["skip_reason"] == "missing_db_url"
    assert report["summary"]["pass"] is False


def test_render_postgres_realdb_schema_probe_markdown_shows_engine() -> None:
    report = {
        "status": "pass",
        "database": "app",
        "graph": "target/app.schemaonly.semsql",
        "high_risk_schema": False,
        "safety_mode": "schema-only extraction; count-only execution",
        "summary": {
            "questions": 1,
            "routed": 1,
            "required_questions": 1,
            "required_ok": 1,
            "count_only_routes": 1,
            "executed_count_only_queries": 1,
            "analytics_questions": 0,
            "analytics_ok": 0,
            "executed_governed_analytics_queries": 0,
            "execution_errors": 0,
            "safe_not_executed": 0,
            "semantic_ok_or_safe_not_executed": 1,
            "needs_review": 0,
            "sample_value_rows": 0,
            "stages": {"stage_0a": 1},
            "pass": True,
            "skipped": False,
        },
        "records": [
            {
                "index": 1,
                "question": "how many orders",
                "stage": "stage_0a",
                "expected_table": "orders",
                "expected_kind": "table_count",
                "expected_field": None,
                "actual_shape": "table_count",
                "actual_table": "orders",
                "actual_field": None,
                "actual_count_table": "orders",
                "count_only": True,
                "executed": True,
                "exec_status": "ok",
                "review": "ok",
                "sql": 'SELECT COUNT(*) FROM "orders"',
            }
        ],
    }

    rendered = render_postgres_realdb_schema_probe_markdown(report)

    assert "# Real DB Postgres Schema-Only Probe" in rendered
    assert 'SELECT COUNT(*) FROM "orders"' in rendered


def test_render_postgres_realdb_schema_probe_suite_markdown_shows_engine() -> None:
    report = {
        "status": "pass",
        "seeds": [1],
        "safety_mode": "schema-only extraction; count-only execution",
        "summary": {
            "run_total": 1,
            "run_passed": 1,
            "run_skipped": 0,
            "run_failed_or_error": 0,
            "databases": ["app"],
            "questions": 1,
            "required_questions": 1,
            "required_ok": 1,
            "count_only_routes": 1,
            "executed_count_only_queries": 1,
            "analytics_questions": 0,
            "analytics_ok": 0,
            "executed_governed_analytics_queries": 0,
            "execution_errors": 0,
            "safe_not_executed": 0,
            "semantic_ok_or_safe_not_executed": 1,
            "needs_review": 0,
            "sample_value_rows": 0,
        },
        "runs": [
            {
                "status": "pass",
                "seed": 1,
                "database": "app",
                "summary": {
                    "questions": 1,
                    "count_only_routes": 1,
                    "executed_count_only_queries": 1,
                    "safe_not_executed": 0,
                    "needs_review": 0,
                    "sample_value_rows": 0,
                },
            }
        ],
    }

    rendered = render_postgres_realdb_schema_probe_suite_markdown(report)

    assert "# Real DB Postgres Schema-Only Probe Suite" in rendered


def test_postgres_url_with_database_replaces_path() -> None:
    url = _postgres_url_with_database(
        "postgres://reporter:pw@db.example:5432/template1?sslmode=require",
        "app",
    )

    assert url == "postgres://reporter:pw@db.example:5432/app?sslmode=require"


def test_classify_governed_sql_accepts_postgres_identifier_quotes() -> None:
    shape = _classify_governed_sql(
        'SELECT AVG("orders"."amount") AS "avg_amount" FROM "orders"'
    )

    assert shape is not None
    assert shape.kind == "avg"
    assert shape.table == "orders"
    assert shape.field == "amount"


def test_realdb_schema_probe_suite_summary_counts_runs() -> None:
    runs = [
        {
            "status": "pass",
            "seed": 1,
            "database": "app",
            "high_risk_schema": True,
            "summary": {
                "questions": 12,
                "required_questions": 12,
                "required_ok": 12,
                "routed": 10,
                "count_only_routes": 10,
                "executed_count_only_queries": 10,
                "analytics_questions": 0,
                "analytics_ok": 0,
                "executed_governed_analytics_queries": 0,
                "execution_errors": 0,
                "safe_not_executed": 2,
                "semantic_ok_or_safe_not_executed": 12,
                "needs_review": 0,
                "sample_value_rows": 0,
                "pass": True,
                "skipped": False,
            },
        },
        {
            "status": "fail",
            "seed": 2,
            "database": "billing",
            "high_risk_schema": False,
            "summary": {
                "questions": 12,
                "required_questions": 12,
                "required_ok": 11,
                "routed": 9,
                "count_only_routes": 9,
                "executed_count_only_queries": 9,
                "analytics_questions": 2,
                "analytics_ok": 1,
                "executed_governed_analytics_queries": 1,
                "execution_errors": 1,
                "safe_not_executed": 3,
                "semantic_ok_or_safe_not_executed": 11,
                "needs_review": 1,
                "sample_value_rows": 0,
                "pass": False,
                "skipped": False,
            },
        },
    ]

    summary = _summarize_suite_runs(runs)

    assert summary["run_total"] == 2
    assert summary["run_passed"] == 1
    assert summary["run_failed_or_error"] == 1
    assert summary["databases"] == ["app", "billing"]
    assert summary["questions"] == 24
    assert summary["required_questions"] == 24
    assert summary["required_ok"] == 23
    assert summary["analytics_questions"] == 2
    assert summary["analytics_ok"] == 1
    assert summary["execution_errors"] == 1
    assert summary["pass"] is False


def test_render_mysql_realdb_schema_probe_suite_markdown_shows_runs() -> None:
    report = {
        "status": "pass",
        "seeds": [1, 2],
        "safety_mode": "schema-only extraction; count-only execution",
        "summary": {
            "run_total": 2,
            "run_passed": 2,
            "run_skipped": 0,
            "run_failed_or_error": 0,
            "databases": ["app"],
            "questions": 24,
            "required_questions": 24,
            "required_ok": 24,
            "count_only_routes": 20,
            "executed_count_only_queries": 20,
            "analytics_questions": 0,
            "analytics_ok": 0,
            "executed_governed_analytics_queries": 0,
            "execution_errors": 0,
            "safe_not_executed": 4,
            "semantic_ok_or_safe_not_executed": 24,
            "needs_review": 0,
            "sample_value_rows": 0,
        },
        "runs": [
            {
                "status": "pass",
                "seed": 1,
                "database": "app",
                "summary": {
                    "questions": 12,
                    "count_only_routes": 10,
                    "executed_count_only_queries": 10,
                    "safe_not_executed": 2,
                    "needs_review": 0,
                    "sample_value_rows": 0,
                },
            },
            {
                "status": "pass",
                "seed": 2,
                "database": "app",
                "summary": {
                    "questions": 12,
                    "count_only_routes": 10,
                    "executed_count_only_queries": 10,
                    "safe_not_executed": 2,
                    "needs_review": 0,
                    "sample_value_rows": 0,
                },
            },
        ],
    }

    rendered = render_mysql_realdb_schema_probe_suite_markdown(report)

    assert "status: `PASS`" in rendered
    assert "runs passed: `2/2`" in rendered
    assert "| `1` | `PASS` | `app` |" in rendered


def test_realdb_schema_probe_helpers_are_privacy_biased() -> None:
    assert humanize_identifier("personal_access_tokens") == "personal access tokens"
    assert name_looks_sensitive("two_factor_authentications")
    assert name_looks_sensitive("password_reset_tokens")
    assert not name_looks_sensitive("orders")
    assert (
        redact_db_url("mysql://root:pw@127.0.0.1:3306/app")
        == "mysql://root:***@127.0.0.1:3306/app"
    )


def test_select_count_tables_includes_sensitive_tables_for_contract() -> None:
    tables = [
        RealDbTable(database="app", table="users"),
        RealDbTable(database="app", table="orders"),
        RealDbTable(database="app", table="password_reset_tokens"),
        RealDbTable(database="app", table="personal_access_tokens"),
    ]

    selected = _select_count_tables(tables, seed=1, sample_size=3)

    assert len(selected) == 3
    assert any(table.sensitive for table in selected)


def test_ambiguous_physical_family_tables_match_runtime_shard_contract() -> None:
    tables = [
        RealDbTable(database="app", table="mail_aliases"),
        RealDbTable(database="app", table="mail_aliases_organizations_1"),
        RealDbTable(database="app", table="mail_aliases_organizations_2"),
        RealDbTable(database="app", table="mail_headers"),
    ]

    ambiguous = _ambiguous_physical_family_tables(tables)

    assert ambiguous == {
        "mail_aliases",
        "mail_aliases_organizations_1",
        "mail_aliases_organizations_2",
    }


def test_expected_reject_probe_scores_safe_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Result:
        sql = None
        stage_pinned = "needs_model"
        error_detail = None

    monkeypatch.setattr(
        realdb_schema_probe,
        "run_cascade_query",
        lambda *args, **kwargs: Result(),
    )

    record = _run_probe_question(
        index=1,
        question={
            "question": "how many mail aliases",
            "expected_table": None,
            "expected_reject_probe": True,
            "required": True,
        },
        semsql_bin=tmp_path / "semsql",
        graph_path=tmp_path / "graph.semsql",
        conn=object(),
        query_timeout_seconds=1,
        exec_timeout_seconds=1.0,
    )

    assert record["expected_reject_probe"] is True
    assert record["review"] == "expected_not_executed"
    assert record["semantic_ok_or_safe_not_executed"] is True
    assert record["exec_status"] == "query_rejected_not_executed"


def test_expected_reject_probe_flags_unexpected_sql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Result:
        sql = "SELECT COUNT(*) FROM `mail_aliases`"
        stage_pinned = "stage_0a"
        error_detail = None

    monkeypatch.setattr(
        realdb_schema_probe,
        "run_cascade_query",
        lambda *args, **kwargs: Result(),
    )

    record = _run_probe_question(
        index=1,
        question={
            "question": "how many mail aliases",
            "expected_table": None,
            "expected_reject_probe": True,
            "required": True,
        },
        semsql_bin=tmp_path / "semsql",
        graph_path=tmp_path / "graph.semsql",
        conn=object(),
        query_timeout_seconds=1,
        exec_timeout_seconds=1.0,
    )

    assert record["executed"] is False
    assert record["review"] == "needs_review"
    assert record["semantic_ok_or_safe_not_executed"] is False
    assert record["exec_status"] == "unexpected_sql_not_executed"


def test_realdb_schema_probe_classifies_governed_sql_shapes() -> None:
    count_shape = _classify_governed_sql("SELECT COUNT(*) FROM `users`")
    assert count_shape is not None
    assert count_shape.kind == "table_count"
    assert count_shape.table == "users"

    date_shape = _classify_governed_sql(
        "SELECT COUNT(*) FROM `users` WHERE `users`.`created_at` = '2026-06-03'"
    )
    assert date_shape is not None
    assert _shape_matches_question(
        date_shape,
        expected_kind="date_count",
        expected_table="users",
        expected_field="created_at",
        expected_literal="2026-06-03",
    )

    group_shape = _classify_governed_sql(
        "SELECT `users`.`active`, COUNT(`users`.`id`) AS `user_count` "
        "FROM `users` GROUP BY `users`.`active` ORDER BY `user_count` DESC"
    )
    assert group_shape is not None
    assert group_shape.kind == "group_count"
    assert group_shape.table == "users"
    assert group_shape.field == "active"

    joined_group_shape = _classify_governed_sql(
        "SELECT `users`.`active`, COUNT(`users`.`id`) AS `user_count` "
        "FROM `users` INNER JOIN `tokens` ON `users`.`id` = `tokens`.`user_id` "
        "GROUP BY `users`.`active`"
    )
    assert joined_group_shape is None

    avg_shape = _classify_governed_sql(
        "SELECT AVG(`migrations`.`batch`) AS `avg_batch` FROM `migrations` "
        "WHERE `migrations`.`batch` IS NOT NULL"
    )
    assert avg_shape is not None
    assert avg_shape.kind == "avg"
    assert avg_shape.table == "migrations"
    assert avg_shape.field == "batch"


def test_select_analytics_questions_derive_from_schema_not_static_examples() -> None:
    tables = [
        RealDbTable(database="app", table="orders"),
        RealDbTable(database="app", table="users"),
    ]
    columns = [
        RealDbColumn("app", "orders", "created_at", "timestamp", "", "YES"),
        RealDbColumn("app", "orders", "status", "enum", "", "NO"),
        RealDbColumn("app", "orders", "amount", "decimal", "", "NO"),
        RealDbColumn("app", "orders", "generated_by", "int", "", "NO"),
        RealDbColumn("app", "orders", "created_date", "int", "", "NO"),
        RealDbColumn("app", "users", "password", "varchar", "", "NO"),
        RealDbColumn("app", "users", "user_id", "bigint", "", "NO"),
    ]

    questions = _select_analytics_questions(
        tables,
        columns,
        seed=7,
        probe_count=5,
    )

    assert {question["expected_kind"] for question in questions} == {
        "date_count",
        "group_count",
        "avg",
    }
    assert all(question["analytics_probe"] for question in questions)
    assert all(question["required"] is False for question in questions)
    assert not any(question.get("expected_field") == "password" for question in questions)
    assert not any(question.get("expected_field") == "user_id" for question in questions)
    assert not any(
        question.get("expected_field") in {"generated_by", "created_date"}
        for question in questions
    )


def test_select_typed_fallback_rate_questions_use_boolean_schema_roles() -> None:
    tables = [
        RealDbTable(database="app", table="mail_users"),
        RealDbTable(database="app", table="transactions"),
    ]
    columns = [
        RealDbColumn("app", "mail_users", "active", "tinyint", "", "NO"),
        RealDbColumn("app", "mail_users", "level", "tinyint", "", "NO"),
        RealDbColumn("app", "mail_users", "attempts", "tinyint", "", "NO"),
        RealDbColumn("app", "transactions", "is_internal", "tinyint", "", "NO"),
        RealDbColumn("app", "transactions", "threat_level", "tinyint", "", "NO"),
        RealDbColumn("app", "transactions", "final_approval", "tinyint", "", "NO"),
        RealDbColumn("app", "transactions", "user_id", "bigint", "", "NO"),
    ]

    questions = select_typed_fallback_rate_questions(
        tables,
        columns,
        seed=3,
        probe_count=10,
    )

    selected_fields = {question["expected_field"] for question in questions}
    assert {"active", "is_internal"}.issubset(selected_fields)
    assert "level" not in selected_fields
    assert "attempts" not in selected_fields
    assert "threat_level" not in selected_fields
    assert "user_id" not in selected_fields
    assert all(question["expected_kind"] == "conditional_rate" for question in questions)
    assert all(question["required"] is True for question in questions)


def test_select_typed_fallback_grouped_metric_questions_pair_metric_and_dimension() -> None:
    tables = [
        RealDbTable(database="app", table="fraud_reports"),
        RealDbTable(database="app", table="geo_events"),
    ]
    columns = [
        RealDbColumn("app", "fraud_reports", "amount", "decimal", "", "NO"),
        RealDbColumn("app", "fraud_reports", "status", "enum", "", "NO"),
        RealDbColumn("app", "fraud_reports", "is_closed", "tinyint", "", "NO"),
        RealDbColumn("app", "fraud_reports", "blacklisted_by", "bigint", "", "NO"),
        RealDbColumn("app", "geo_events", "latitude", "decimal", "", "NO"),
        RealDbColumn("app", "geo_events", "status", "enum", "", "NO"),
    ]

    questions = select_typed_fallback_grouped_metric_questions(
        tables,
        columns,
        seed=11,
        probe_count=10,
    )

    selected = {
        (question["expected_metric_field"], question["expected_group_field"])
        for question in questions
    }
    assert ("amount", "status") in selected
    assert ("amount", "is_closed") not in selected
    assert not any(metric == "blacklisted_by" for metric, _ in selected)
    assert not any(metric == "latitude" for metric, _ in selected)
    assert all(question["expected_kind"] == "grouped_avg" for question in questions)
    assert all("highest average" in question["question"] for question in questions)

    boolean_dimension_questions = select_typed_fallback_grouped_metric_questions(
        [RealDbTable(database="app", table="website_pages")],
        [
            RealDbColumn("app", "website_pages", "page_order", "int", "", "NO"),
            RealDbColumn("app", "website_pages", "is_published", "tinyint", "", "NO"),
        ],
        seed=1,
        probe_count=1,
    )
    assert boolean_dimension_questions == []


def test_select_typed_fallback_multi_series_metric_questions_use_time_and_group() -> None:
    tables = [RealDbTable(database="app", table="campaign_events")]
    columns = [
        RealDbColumn("app", "campaign_events", "score", "decimal", "", "NO"),
        RealDbColumn("app", "campaign_events", "created_at", "timestamp", "", "NO"),
        RealDbColumn("app", "campaign_events", "channel", "enum", "", "NO"),
        RealDbColumn("app", "campaign_events", "has_error", "tinyint", "", "NO"),
        RealDbColumn("app", "campaign_events", "owner_id", "bigint", "MUL", "NO"),
    ]

    questions = select_typed_fallback_multi_series_metric_questions(
        tables,
        columns,
        seed=13,
        probe_count=10,
    )

    assert questions
    assert {
        (
            question["expected_metric_field"],
            question["expected_time_field"],
            question["expected_group_field"],
        )
        for question in questions
    } == {("score", "created_at", "channel")}
    assert all(
        question["expected_kind"] == "multi_series_grouped_avg"
        for question in questions
    )
    assert all(" over " in question["question"] for question in questions)
    assert all("has_error" not in question["question"] for question in questions)


def test_select_typed_fallback_filtered_grouped_metric_questions_include_filter() -> None:
    tables = [RealDbTable(database="app", table="fraud_reports")]
    columns = [
        RealDbColumn("app", "fraud_reports", "amount", "decimal", "", "NO"),
        RealDbColumn("app", "fraud_reports", "final_status", "enum", "", "NO"),
        RealDbColumn("app", "fraud_reports", "is_closed", "tinyint", "", "NO"),
        RealDbColumn("app", "fraud_reports", "has_police_report", "tinyint", "", "NO"),
        RealDbColumn("app", "fraud_reports", "closed_by", "bigint", "", "YES"),
    ]

    questions = select_typed_fallback_filtered_grouped_metric_questions(
        tables,
        columns,
        seed=5,
        probe_count=10,
    )

    selected = {
        (
            question["expected_metric_field"],
            question["expected_group_field"],
            question["expected_filter_field"],
        )
        for question in questions
    }
    assert ("amount", "final_status", "is_closed") in selected
    assert ("amount", "final_status", "has_police_report") in selected
    assert not any(filter_field == "closed_by" for _, _, filter_field in selected)
    assert all(question["expected_kind"] == "filtered_grouped_avg" for question in questions)
    assert all(" that " in question["question"] for question in questions)


def test_select_typed_fallback_value_filtered_grouped_metric_questions_use_safe_samples() -> None:
    tables = [RealDbTable(database="app", table="campaign_events")]
    columns = [
        RealDbColumn("app", "campaign_events", "score", "decimal", "", "NO"),
        RealDbColumn("app", "campaign_events", "status", "enum", "", "NO"),
        RealDbColumn("app", "campaign_events", "channel", "varchar", "", "NO"),
        RealDbColumn("app", "campaign_events", "customer_email", "varchar", "", "NO"),
        RealDbColumn("app", "campaign_events", "created_at", "timestamp", "", "NO"),
    ]

    questions = select_typed_fallback_value_filtered_grouped_metric_questions(
        tables,
        columns,
        sample_values={
            "campaign_events.channel": [
                "paid_search",
                "customer@example.com",
                "00000000-0000-0000-0000-000000000000",
                "12345",
            ],
            "campaign_events.customer_email": ["billing@example.com"],
            "campaign_events.created_at": ["2026-06-04"],
        },
        seed=5,
        probe_count=10,
    )

    assert questions
    assert {
        (
            question["expected_metric_field"],
            question["expected_group_field"],
            question["expected_filter_field"],
            question["expected_filter_value"],
        )
        for question in questions
    } == {("score", "status", "channel", "paid_search")}
    assert all(
        question["expected_kind"] == "value_filtered_grouped_avg"
        for question in questions
    )
    assert all(question["sample_backed_filter"] is True for question in questions)
    assert all("'paid_search'" in question["question"] for question in questions)


def test_select_typed_fallback_joined_filtered_grouped_metric_questions_use_fk() -> None:
    tables = [
        RealDbTable(database="app", table="orders"),
        RealDbTable(database="app", table="customers"),
    ]
    columns = [
        RealDbColumn("app", "orders", "amount", "decimal", "", "NO"),
        RealDbColumn("app", "orders", "is_paid", "tinyint", "", "NO"),
        RealDbColumn("app", "orders", "customer_id", "bigint", "MUL", "NO"),
        RealDbColumn("app", "customers", "id", "bigint", "PRI", "NO"),
        RealDbColumn("app", "customers", "segment", "enum", "", "NO"),
        RealDbColumn("app", "customers", "company_name", "varchar", "", "NO"),
    ]
    relationships = [
        RealDbRelationship(
            database="app",
            table="orders",
            column="customer_id",
            referenced_table="customers",
            referenced_column="id",
        )
    ]

    questions = select_typed_fallback_joined_filtered_grouped_metric_questions(
        tables,
        columns,
        relationships,
        seed=7,
        probe_count=10,
    )

    assert questions
    assert all(question["expected_kind"] == "joined_filtered_grouped_avg" for question in questions)
    assert {
        (
            question["expected_metric_table"],
            question["expected_metric_field"],
            question["expected_group_table"],
            question["expected_group_field"],
            question["expected_join_table"],
            question["expected_join_field"],
            question["expected_join_ref_table"],
            question["expected_join_ref_field"],
            question["expected_filter_field"],
        )
        for question in questions
    } >= {
        (
            "orders",
            "amount",
            "customers",
            "segment",
            "orders",
            "customer_id",
            "customers",
            "id",
            "is_paid",
        )
    }
    assert all("customer" in question["question"] for question in questions)


def test_select_typed_fallback_multi_joined_grouped_metric_questions_use_two_hops() -> None:
    tables = [
        RealDbTable(database="app", table="orders"),
        RealDbTable(database="app", table="customers"),
        RealDbTable(database="app", table="regions"),
    ]
    columns = [
        RealDbColumn("app", "orders", "amount", "decimal", "", "NO"),
        RealDbColumn("app", "orders", "is_paid", "tinyint", "", "NO"),
        RealDbColumn("app", "orders", "customer_id", "bigint", "MUL", "NO"),
        RealDbColumn("app", "customers", "id", "bigint", "PRI", "NO"),
        RealDbColumn("app", "customers", "region_id", "bigint", "MUL", "NO"),
        RealDbColumn("app", "regions", "id", "bigint", "PRI", "NO"),
        RealDbColumn("app", "regions", "name", "varchar", "", "NO"),
        RealDbColumn("app", "regions", "segment", "enum", "", "NO"),
    ]
    relationships = [
        RealDbRelationship(
            database="app",
            table="orders",
            column="customer_id",
            referenced_table="customers",
            referenced_column="id",
        ),
        RealDbRelationship(
            database="app",
            table="customers",
            column="region_id",
            referenced_table="regions",
            referenced_column="id",
        ),
    ]

    questions = select_typed_fallback_multi_joined_filtered_grouped_metric_questions(
        tables,
        columns,
        relationships,
        seed=11,
        probe_count=10,
    )

    assert questions
    assert all(
        question["expected_kind"] == "multi_joined_filtered_grouped_avg"
        for question in questions
    )
    selected = {
        (
            question["expected_metric_table"],
            question["expected_metric_field"],
            question["expected_group_table"],
            question["expected_group_field"],
            tuple(
                (
                    step["left_table"],
                    step["left_field"],
                    step["right_table"],
                    step["right_field"],
                )
                for step in question["expected_join_path"]
            ),
            question["expected_filter_field"],
        )
        for question in questions
    }
    assert (
        "orders",
        "amount",
        "regions",
        "name",
        (
            ("orders", "customer_id", "customers", "id"),
            ("customers", "region_id", "regions", "id"),
        ),
        "is_paid",
    ) in selected
    assert all("regions" in question["question"] for question in questions)


def test_select_typed_fallback_multi_joined_grouped_metric_questions_skip_direct_path() -> None:
    tables = [
        RealDbTable(database="app", table="orders"),
        RealDbTable(database="app", table="customers"),
        RealDbTable(database="app", table="regions"),
    ]
    columns = [
        RealDbColumn("app", "orders", "amount", "decimal", "", "NO"),
        RealDbColumn("app", "orders", "customer_id", "bigint", "MUL", "NO"),
        RealDbColumn("app", "orders", "region_id", "bigint", "MUL", "NO"),
        RealDbColumn("app", "customers", "id", "bigint", "PRI", "NO"),
        RealDbColumn("app", "customers", "region_id", "bigint", "MUL", "NO"),
        RealDbColumn("app", "regions", "id", "bigint", "PRI", "NO"),
        RealDbColumn("app", "regions", "name", "varchar", "", "NO"),
    ]
    relationships = [
        RealDbRelationship("app", "orders", "customer_id", "customers", "id"),
        RealDbRelationship("app", "customers", "region_id", "regions", "id"),
        RealDbRelationship("app", "orders", "region_id", "regions", "id"),
    ]

    questions = select_typed_fallback_multi_joined_filtered_grouped_metric_questions(
        tables,
        columns,
        relationships,
        seed=11,
        probe_count=10,
    )

    assert questions == []


def test_select_typed_fallback_multi_joined_grouped_metric_questions_skip_raw_ambiguous_paths() -> None:
    tables = [
        RealDbTable(database="app", table="reports"),
        RealDbTable(database="app", table="entities"),
        RealDbTable(database="app", table="users"),
        RealDbTable(database="app", table="banks"),
    ]
    columns = [
        RealDbColumn("app", "reports", "amount", "decimal", "", "NO"),
        RealDbColumn("app", "reports", "entity_id", "bigint", "MUL", "NO"),
        RealDbColumn("app", "reports", "user_id", "bigint", "MUL", "NO"),
        RealDbColumn("app", "entities", "id", "bigint", "PRI", "NO"),
        RealDbColumn("app", "entities", "bank_id", "bigint", "MUL", "NO"),
        RealDbColumn("app", "users", "id", "bigint", "PRI", "NO"),
        RealDbColumn("app", "users", "bank_id", "bigint", "MUL", "NO"),
        RealDbColumn("app", "banks", "id", "bigint", "PRI", "NO"),
        RealDbColumn("app", "banks", "name", "varchar", "", "NO"),
    ]
    relationships = [
        RealDbRelationship("app", "reports", "entity_id", "entities", "id"),
        RealDbRelationship("app", "entities", "bank_id", "banks", "id"),
        RealDbRelationship("app", "reports", "user_id", "users", "id"),
        RealDbRelationship("app", "users", "bank_id", "banks", "id"),
    ]

    questions = select_typed_fallback_multi_joined_filtered_grouped_metric_questions(
        tables,
        columns,
        relationships,
        seed=11,
        probe_count=10,
    )

    assert questions == []


def test_typed_fallback_grouped_metric_questions_skip_boolean_group_dimensions() -> None:
    tables = [RealDbTable(database="app", table="organizations")]
    columns = [
        RealDbColumn("app", "organizations", "max_ai_write", "integer", "", "NO"),
        RealDbColumn("app", "organizations", "is_active", "tinyint", "", "NO"),
        RealDbColumn("app", "organizations", "status", "enum", "", "NO"),
    ]

    questions = select_typed_fallback_grouped_metric_questions(
        tables,
        columns,
        seed=3,
        probe_count=10,
    )

    assert questions
    assert {question["expected_group_field"] for question in questions} == {"status"}


def test_typed_fallback_joined_metric_questions_skip_internal_owner_dimensions() -> None:
    tables = [
        RealDbTable(database="app", table="campaign_links"),
        RealDbTable(database="app", table="campaigns"),
    ]
    columns = [
        RealDbColumn("app", "campaign_links", "clicks", "integer", "", "NO"),
        RealDbColumn("app", "campaign_links", "campaign_id", "bigint", "MUL", "NO"),
        RealDbColumn("app", "campaigns", "id", "bigint", "PRI", "NO"),
        RealDbColumn("app", "campaigns", "created_by", "char", "", "NO"),
        RealDbColumn("app", "campaigns", "status", "enum", "", "NO"),
    ]
    relationships = [
        RealDbRelationship("app", "campaign_links", "campaign_id", "campaigns", "id")
    ]

    questions = select_typed_fallback_joined_filtered_grouped_metric_questions(
        tables,
        columns,
        relationships,
        seed=3,
        probe_count=10,
    )

    assert questions
    assert {question["expected_group_field"] for question in questions} == {"status"}


def test_typed_fallback_joined_metric_questions_skip_artifact_code_dimensions() -> None:
    tables = [
        RealDbTable(database="app", table="orders"),
        RealDbTable(database="app", table="plans"),
    ]
    columns = [
        RealDbColumn("app", "orders", "amount", "decimal", "", "NO"),
        RealDbColumn("app", "orders", "plan_id", "bigint", "MUL", "NO"),
        RealDbColumn("app", "plans", "id", "bigint", "PRI", "NO"),
        RealDbColumn("app", "plans", "code", "varchar", "", "NO"),
        RealDbColumn("app", "plans", "y_code", "varchar", "", "NO"),
    ]
    relationships = [RealDbRelationship("app", "orders", "plan_id", "plans", "id")]

    questions = select_typed_fallback_joined_filtered_grouped_metric_questions(
        tables,
        columns,
        relationships,
        seed=3,
        probe_count=10,
    )

    assert questions
    assert {question["expected_group_field"] for question in questions} == {"code"}
