"""Tests for the Spider mini-corpus fixture builder."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from semsql_eval.fixtures import (
    MINI_CORPUS,
    build_corpus,
    build_queryframe_canary,
    write_queryframe_canary_mysql_sql,
    write_queryframe_canary_postgres_sql,
)
from semsql_eval.spider import SpiderSuite


def test_build_corpus_writes_valid_layout(tmp_path: Path) -> None:
    out = build_corpus(tmp_path / "spider")
    assert (out / "dev.json").exists()
    assert (out / "tables.json").exists()
    assert (out / "database").is_dir()
    for db in MINI_CORPUS.dbs:
        sqlite_path = out / "database" / db.db_id / f"{db.db_id}.sqlite"
        assert sqlite_path.exists()
        assert sqlite_path.stat().st_size > 0


def test_built_corpus_is_loadable_by_spider_suite(tmp_path: Path) -> None:
    out = build_corpus(tmp_path / "spider")
    suite = SpiderSuite.load(out / "dev.json", out / "database")
    assert len(suite.examples) == len(MINI_CORPUS.examples)
    db_ids = {ex.db_id for ex in suite.examples}
    assert db_ids == {db.db_id for db in MINI_CORPUS.dbs}


def test_built_sqlite_files_have_expected_rows(tmp_path: Path) -> None:
    out = build_corpus(tmp_path / "spider")
    for db in MINI_CORPUS.dbs:
        sqlite_path = out / "database" / db.db_id / f"{db.db_id}.sqlite"
        conn = sqlite3.connect(sqlite_path)
        try:
            for t in db.tables:
                cur = conn.execute(f"SELECT count(*) FROM {t.name}")
                count = cur.fetchone()[0]
                assert count == len(t.rows), (
                    f"{db.db_id}.{t.name}: expected {len(t.rows)} rows, got {count}"
                )
        finally:
            conn.close()


def test_build_corpus_is_idempotent(tmp_path: Path) -> None:
    out = build_corpus(tmp_path / "spider")
    first_dev = (out / "dev.json").read_bytes()
    # Second call must overwrite cleanly without any DB-locked errors
    # (writer drops + recreates each table) and produce identical
    # output.
    out2 = build_corpus(tmp_path / "spider")
    assert out2 == out
    assert (out / "dev.json").read_bytes() == first_dev


def test_dev_json_examples_match_spec_order(tmp_path: Path) -> None:
    out = build_corpus(tmp_path / "spider")
    dev = json.loads((out / "dev.json").read_text(encoding="utf-8"))
    assert len(dev) == len(MINI_CORPUS.examples)
    for got, want in zip(dev, MINI_CORPUS.examples, strict=True):
        assert got["db_id"] == want.db_id
        assert got["question"] == want.question
        assert got["query"] == want.gold_sql


def test_tables_json_lists_every_table(tmp_path: Path) -> None:
    out = build_corpus(tmp_path / "spider")
    tables = json.loads((out / "tables.json").read_text(encoding="utf-8"))
    by_id = {entry["db_id"]: entry for entry in tables}
    for db in MINI_CORPUS.dbs:
        assert db.db_id in by_id
        spec_tables = {t.name for t in db.tables}
        assert set(by_id[db.db_id]["table_names"]) == spec_tables


def test_queryframe_canary_writes_seeded_layout(tmp_path: Path) -> None:
    first = build_queryframe_canary(tmp_path / "canary_one", seed=7)
    second = build_queryframe_canary(tmp_path / "canary_two", seed=8)

    assert (first / "dev.json").exists()
    assert (first / "tables.json").exists()
    assert (first / "queryframe_canary.json").exists()
    assert (first / "dev.json").read_text(encoding="utf-8") != (
        second / "dev.json"
    ).read_text(encoding="utf-8")

    metadata = json.loads((first / "queryframe_canary.json").read_text())
    routed_kinds = {case["kind"] for case in metadata["routed_cases"]}
    assert {
        "enum_filter",
        "join_count_filter",
        "join_value_aggregate",
        "paraphrase_join_value_aggregate",
        "paraphrase_join_aggregate",
        "topk_group_aggregate",
        "paraphrase_topk_group_aggregate",
        "structured_literal_zip",
        "paraphrase_structured_literal_zip",
        "structured_literal_code",
        "paraphrase_structured_literal_code",
        "structured_literal_date",
    } <= routed_kinds
    assert metadata["reject_cases"]
    assert all(case["should_route"] is False for case in metadata["reject_cases"])


def test_queryframe_canary_alias_variant_changes_schema_names(tmp_path: Path) -> None:
    out = build_queryframe_canary(tmp_path / "canary", seed=7, variant="alias")

    metadata = json.loads((out / "queryframe_canary.json").read_text())
    tables = json.loads((out / "tables.json").read_text(encoding="utf-8"))
    [entry] = tables

    assert metadata["variant"] == "alias"
    assert entry["db_id"] == "commerce_alias_canary"
    assert {"clients", "territories", "catalog_items", "transactions"} <= set(
        entry["table_names"]
    )
    assert any("clients" in case["question"] for case in metadata["routed_cases"])
    assert any("transactions" in case["question"] for case in metadata["routed_cases"])


def test_queryframe_canary_random_alias_variant_records_naming_plan(
    tmp_path: Path,
) -> None:
    out = build_queryframe_canary(tmp_path / "canary", seed=7, variant="random_alias")

    metadata = json.loads((out / "queryframe_canary.json").read_text())
    tables = json.loads((out / "tables.json").read_text(encoding="utf-8"))
    [entry] = tables
    naming = metadata["naming_plan"]

    assert metadata["variant"] == "random_alias"
    assert entry["db_id"] == "commerce_random_alias_canary"
    assert {
        naming["account_table"],
        naming["region_table"],
        naming["product_table"],
        naming["order_table"],
    } <= set(entry["table_names"])
    assert naming["account_table"] != "accounts"
    assert naming["region_table"] != "regions"
    assert naming["product_table"] != "products"
    assert naming["order_table"] != "orders"
    assert any(naming["account_term"] in case["question"] for case in metadata["routed_cases"])
    assert any(naming["order_term"] in case["question"] for case in metadata["routed_cases"])


def test_queryframe_canary_gold_sql_executes(tmp_path: Path) -> None:
    out = build_queryframe_canary(tmp_path / "canary", seed=11)
    dev = json.loads((out / "dev.json").read_text(encoding="utf-8"))

    for row in dev:
        sqlite_path = out / "database" / row["db_id"] / f"{row['db_id']}.sqlite"
        conn = sqlite3.connect(sqlite_path)
        try:
            conn.execute(row["query"]).fetchall()
        finally:
            conn.close()


def test_queryframe_canary_exposes_fk_metadata(tmp_path: Path) -> None:
    out = build_queryframe_canary(tmp_path / "canary", seed=13)
    tables = json.loads((out / "tables.json").read_text(encoding="utf-8"))
    [entry] = tables
    assert entry["primary_keys"]
    assert len(entry["foreign_keys"]) == 3


def test_queryframe_canary_writes_postgres_setup_sql(tmp_path: Path) -> None:
    out = tmp_path / "canary"
    build_queryframe_canary(out, seed=13, variant="alias")
    paths = write_queryframe_canary_postgres_sql(
        out,
        seed=13,
        variant="alias",
        schema="semsql_qf_test",
    )

    setup = paths["setup"].read_text(encoding="utf-8")
    teardown = paths["teardown"].read_text(encoding="utf-8")

    assert 'CREATE SCHEMA "semsql_qf_test"' in setup
    assert 'CREATE TABLE "semsql_qf_test"."clients"' in setup
    assert 'REFERENCES "semsql_qf_test"."territories"("id")' in setup
    assert 'INSERT INTO "semsql_qf_test"."transactions"' in setup
    assert 'DROP SCHEMA IF EXISTS "semsql_qf_test" CASCADE' in teardown


def test_queryframe_canary_writes_mysql_setup_sql(tmp_path: Path) -> None:
    out = tmp_path / "canary"
    build_queryframe_canary(out, seed=13, variant="alias")
    paths = write_queryframe_canary_mysql_sql(
        out,
        seed=13,
        variant="alias",
        database="semsql_qf_test",
    )

    setup = paths["setup"].read_text(encoding="utf-8")
    teardown = paths["teardown"].read_text(encoding="utf-8")

    assert "CREATE DATABASE `semsql_qf_test`" in setup
    assert "CREATE TABLE `clients`" in setup
    assert "REFERENCES `territories`(`id`)" in setup
    assert "INSERT INTO `transactions`" in setup
    assert "DROP DATABASE IF EXISTS `semsql_qf_test`" in teardown
