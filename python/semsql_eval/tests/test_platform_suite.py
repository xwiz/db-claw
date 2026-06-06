from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from semsql_eval.platform_suite import (
    build_business_analytics_suite,
    build_platform_query_suite,
)


def test_platform_query_suite_writes_sqlite_and_metadata(tmp_path: Path) -> None:
    suite = build_platform_query_suite(tmp_path)

    assert suite["suite"] == "platform-comparison-v1"
    assert (tmp_path / "platform_query_suite.json").exists()
    assert (tmp_path / "questions.jsonl").exists()
    assert (tmp_path / "expected.sql").exists()
    assert (tmp_path / "README.md").exists()
    assert (tmp_path / "dev.json").exists()

    cases = suite["cases"]
    assert isinstance(cases, list)
    dispositions = {case["disposition"] for case in cases}
    assert dispositions == {"route", "clarify", "reject", "known_gap"}
    assert sum(1 for case in cases if case["disposition"] == "route") >= 8
    assert any(case["family"] == "unsafe_action" for case in cases)

    sqlite_path = (
        tmp_path / "database" / "growth_ops" / "growth_ops.sqlite"
    )
    assert sqlite_path.exists()
    conn = sqlite3.connect(sqlite_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    finally:
        conn.close()
    assert count == 4


def test_platform_query_suite_semantic_alias_writes_sidecars_and_rewritten_sql(
    tmp_path: Path,
) -> None:
    suite = build_platform_query_suite(tmp_path, schema_variant="semantic_alias")

    assert suite["schema_variant"] == "semantic_alias"
    assert suite["source_db_id"] == "growth_ops"
    assert suite["db_id"] == "growth_ops_semantic_alias"
    assert (tmp_path / "semantic_alias_vocab.jsonl").exists()
    assert (tmp_path / "database_description" / "clients.csv").exists()

    cases = suite["cases"]
    assert isinstance(cases, list)
    first_route = next(case for case in cases if case["id"] == "pq001")
    assert "FROM clients" in first_route["expected_sql"]
    assert "clients.client_name" in first_route["expected_sql"]
    assert "accounts." not in first_route["expected_sql"]

    vocab_text = (tmp_path / "semantic_alias_vocab.jsonl").read_text(encoding="utf-8")
    assert '"term": "accounts"' in vocab_text
    assert '"entity": "clients"' in vocab_text
    assert '"field": "clients.client_name"' in vocab_text

    sqlite_path = (
        tmp_path
        / "database"
        / "growth_ops_semantic_alias"
        / "growth_ops_semantic_alias.sqlite"
    )
    conn = sqlite3.connect(sqlite_path)
    try:
        old_table_count = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'accounts'"
        ).fetchone()[0]
        client_count = conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
        conn.execute(first_route["expected_sql"]).fetchall()
    finally:
        conn.close()
    assert old_table_count == 0
    assert client_count == 4


def test_platform_query_suite_dev_json_contains_only_route_cases(tmp_path: Path) -> None:
    suite = build_platform_query_suite(tmp_path)
    route_count = sum(
        1
        for case in suite["cases"]
        if isinstance(case, dict) and case["disposition"] == "route"
    )

    dev_json = json.loads((tmp_path / "dev.json").read_text(encoding="utf-8"))

    assert len(dev_json) == route_count
    assert all(row["db_id"] == "growth_ops" for row in dev_json)
    assert all(row["query"] for row in dev_json)


def test_business_analytics_suite_writes_sqlite_and_metadata(tmp_path: Path) -> None:
    suite = build_business_analytics_suite(tmp_path)

    assert suite["suite"] == "business-analytics-v1"
    assert suite["db_id"] == "business_analytics"
    assert (tmp_path / "platform_query_suite.json").exists()
    assert (tmp_path / "questions.jsonl").exists()
    assert (tmp_path / "expected.sql").exists()
    assert (tmp_path / "README.md").exists()
    assert (tmp_path / "dev.json").exists()

    cases = suite["cases"]
    assert isinstance(cases, list)
    dispositions = {case["disposition"] for case in cases}
    assert dispositions == {"route", "clarify", "reject"}
    assert sum(1 for case in cases if case["disposition"] == "route") >= 20
    assert any(case["family"] == "crm_pipeline_topk" for case in cases)
    assert any(case["family"] == "pii_row_dump" for case in cases)

    sqlite_path = (
        tmp_path
        / "database"
        / "business_analytics"
        / "business_analytics.sqlite"
    )
    assert sqlite_path.exists()
    conn = sqlite3.connect(sqlite_path)
    try:
        account_count = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        opportunity_count = conn.execute(
            "SELECT COUNT(*) FROM opportunities"
        ).fetchone()[0]
    finally:
        conn.close()
    assert account_count == 6
    assert opportunity_count == 7


def test_business_analytics_suite_semantic_alias_rewrites_schema_and_sql(
    tmp_path: Path,
) -> None:
    suite = build_business_analytics_suite(tmp_path, schema_variant="semantic_alias")

    assert suite["schema_variant"] == "semantic_alias"
    assert suite["source_db_id"] == "business_analytics"
    assert suite["db_id"] == "business_analytics_semantic_alias"
    assert (tmp_path / "semantic_alias_vocab.jsonl").exists()
    assert (tmp_path / "database_description" / "organizations.csv").exists()

    cases = suite["cases"]
    assert isinstance(cases, list)
    first_route = next(case for case in cases if case["id"] == "ba001")
    assert "FROM organizations" in first_route["expected_sql"]
    assert "organizations.annual_recurring_revenue" in first_route["expected_sql"]
    assert "accounts." not in first_route["expected_sql"]

    sqlite_path = (
        tmp_path
        / "database"
        / "business_analytics_semantic_alias"
        / "business_analytics_semantic_alias.sqlite"
    )
    conn = sqlite3.connect(sqlite_path)
    try:
        org_count = conn.execute("SELECT COUNT(*) FROM organizations").fetchone()[0]
        conn.execute(first_route["expected_sql"]).fetchall()
    finally:
        conn.close()
    assert org_count == 6


def test_business_analytics_suite_random_alias_is_seeded_and_replayable(
    tmp_path: Path,
) -> None:
    first = build_business_analytics_suite(
        tmp_path / "first",
        schema_variant="random_alias",
        schema_alias_seed=42,
    )
    second = build_business_analytics_suite(
        tmp_path / "second",
        schema_variant="random_alias",
        schema_alias_seed=42,
    )
    third = build_business_analytics_suite(
        tmp_path / "third",
        schema_variant="random_alias",
        schema_alias_seed=43,
    )

    assert first["schema_variant"] == "random_alias"
    assert first["schema_alias_seed"] == 42
    assert first["source_db_id"] == "business_analytics"
    assert first["db_id"] == "business_analytics_random_alias_42"
    assert first["schema_alias_plan"] == second["schema_alias_plan"]
    assert first["schema_alias_plan"] != third["schema_alias_plan"]

    plan = first["schema_alias_plan"]
    assert isinstance(plan, dict)
    table_map = plan["table_map"]
    column_map = plan["column_map"]
    assert isinstance(table_map, dict)
    assert isinstance(column_map, dict)
    assert table_map["accounts"].startswith("t_")
    assert table_map["accounts"] != "accounts"
    assert column_map["accounts.company_name"].startswith("c_")

    first_route = next(
        case for case in first["cases"] if isinstance(case, dict) and case["id"] == "ba001"
    )
    assert "accounts." not in first_route["expected_sql"]
    assert str(table_map["accounts"]) in first_route["expected_sql"]
    assert str(column_map["accounts.company_name"]) in first_route["expected_sql"]

    vocab_text = (tmp_path / "first" / "semantic_alias_vocab.jsonl").read_text(
        encoding="utf-8"
    )
    assert '"term": "accounts"' in vocab_text
    assert f'"entity": "{table_map["accounts"]}"' in vocab_text

    description_path = tmp_path / "first" / "database_description" / f"{table_map['accounts']}.csv"
    assert description_path.exists()
    assert "Company Name" in description_path.read_text(encoding="utf-8")

    sqlite_path = (
        tmp_path
        / "first"
        / "database"
        / "business_analytics_random_alias_42"
        / "business_analytics_random_alias_42.sqlite"
    )
    conn = sqlite3.connect(sqlite_path)
    try:
        old_table_count = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'accounts'"
        ).fetchone()[0]
        target_count = conn.execute(f"SELECT COUNT(*) FROM {table_map['accounts']}").fetchone()[0]
        conn.execute(first_route["expected_sql"]).fetchall()
    finally:
        conn.close()
    assert old_table_count == 0
    assert target_count == 6


def test_business_analytics_suite_dev_json_contains_only_route_cases(
    tmp_path: Path,
) -> None:
    suite = build_business_analytics_suite(tmp_path)
    route_count = sum(
        1
        for case in suite["cases"]
        if isinstance(case, dict) and case["disposition"] == "route"
    )

    dev_json = json.loads((tmp_path / "dev.json").read_text(encoding="utf-8"))

    assert len(dev_json) == route_count
    assert all(row["db_id"] == "business_analytics" for row in dev_json)
    assert all(row["query"] for row in dev_json)
