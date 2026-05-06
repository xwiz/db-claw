"""Tests for the Spider mini-corpus fixture builder."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from semsql_eval.fixtures import MINI_CORPUS, build_corpus
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
