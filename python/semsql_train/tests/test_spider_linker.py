"""Tests for `spider_linker`. No GPU required."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from semsql_train.spider_linker import (
    SpiderLinkerConfig,
    extract_referenced_items,
    generate_linker_pairs_from_spider_with_stats,
    load_tables_json,
    write_pairs_jsonl,
)


@pytest.fixture()
def fixture_dir(tmp_path: Path) -> Path:
    tables = [
        {
            "db_id": "school",
            "table_names": ["district", "school"],
            "table_names_original": ["district", "school"],
            "column_names": [[-1, "*"], [0, "id"], [0, "name"], [1, "id"], [1, "name"], [1, "district_id"]],
            "column_names_original": [
                [-1, "*"],
                [0, "id"],
                [0, "name"],
                [1, "id"],
                [1, "name"],
                [1, "district_id"],
            ],
            "primary_keys": [1, 3],
            "foreign_keys": [[5, 1]],
        },
    ]
    questions = [
        {
            "db_id": "school",
            "question": "How many schools per district?",
            "query": "SELECT district_id, COUNT(*) FROM school GROUP BY district_id",
        },
        {
            "db_id": "school",
            "question": "What is the name of district 1?",
            "query": "SELECT name FROM district WHERE id = 1",
        },
        {
            "db_id": "school",
            "question": "Broken query example",
            "query": "this is not sql at all",
        },
    ]
    (tmp_path / "tables.json").write_text(json.dumps(tables), encoding="utf-8")
    (tmp_path / "dev.json").write_text(json.dumps(questions), encoding="utf-8")
    return tmp_path


def test_load_tables_skips_star_pseudocolumn(fixture_dir: Path) -> None:
    schemas = load_tables_json(fixture_dir / "tables.json")
    school = schemas.tables("school")
    by_table = {t.table: t.columns for t in school}
    # `*` (table_index = -1) must not land on either real table.
    assert "*" not in by_table["school"]
    assert "*" not in by_table["district"]
    assert "district_id" in by_table["school"]


def test_extract_references_qualified_and_bare_columns(fixture_dir: Path) -> None:
    schemas = load_tables_json(fixture_dir / "tables.json")
    sql = "SELECT s.name FROM school s JOIN district d ON s.district_id = d.id"
    tables, columns = extract_referenced_items(sql, schemas.tables("school"))
    assert tables == {"school", "district"}
    assert "school.name" in columns
    assert "school.district_id" in columns
    assert "district.id" in columns


def test_generator_emits_positives_and_negatives(fixture_dir: Path) -> None:
    cfg = SpiderLinkerConfig(hard_negatives_per_positive=1, easy_negatives_per_positive=1)
    records, stats = generate_linker_pairs_from_spider_with_stats(
        fixture_dir / "dev.json", fixture_dir / "tables.json", cfg
    )
    # Both parseable questions contribute; the broken one is skipped.
    assert stats["parsed"] == 2
    assert stats["skipped_unparseable"] == 1
    assert stats["positives"] > 0
    assert stats["negatives"] > 0

    # Schema canonical-kind vocabulary matches the SemanticGraph.
    kinds = {r["candidate_kind"] for r in records}
    assert kinds.issubset({"entity", "field"})


def test_generator_is_deterministic(fixture_dir: Path) -> None:
    cfg_a = SpiderLinkerConfig(seed=1234)
    cfg_b = SpiderLinkerConfig(seed=1234)
    a, _ = generate_linker_pairs_from_spider_with_stats(
        fixture_dir / "dev.json", fixture_dir / "tables.json", cfg_a
    )
    b, _ = generate_linker_pairs_from_spider_with_stats(
        fixture_dir / "dev.json", fixture_dir / "tables.json", cfg_b
    )
    assert a == b


def test_generator_is_seed_sensitive(fixture_dir: Path) -> None:
    cfg_a = SpiderLinkerConfig(seed=1, easy_negatives_per_positive=3)
    cfg_b = SpiderLinkerConfig(seed=999_999, easy_negatives_per_positive=3)
    a, _ = generate_linker_pairs_from_spider_with_stats(
        fixture_dir / "dev.json", fixture_dir / "tables.json", cfg_a
    )
    b, _ = generate_linker_pairs_from_spider_with_stats(
        fixture_dir / "dev.json", fixture_dir / "tables.json", cfg_b
    )
    # Easy negatives are shuffled; positive set is identical but the
    # negatives differ, so the corpora are not byte-identical.
    assert a != b


def test_unknown_db_is_skipped_with_stats(fixture_dir: Path, tmp_path: Path) -> None:
    questions = [
        {"db_id": "missing", "question": "x", "query": "SELECT 1"},
    ]
    qpath = tmp_path / "dev.json"
    qpath.write_text(json.dumps(questions), encoding="utf-8")

    cfg = SpiderLinkerConfig()
    records, stats = generate_linker_pairs_from_spider_with_stats(
        qpath, fixture_dir / "tables.json", cfg
    )
    assert records == []
    assert stats["skipped_unknown_db"] == 1


def test_write_pairs_jsonl_round_trip(fixture_dir: Path, tmp_path: Path) -> None:
    cfg = SpiderLinkerConfig()
    records, _ = generate_linker_pairs_from_spider_with_stats(
        fixture_dir / "dev.json", fixture_dir / "tables.json", cfg
    )
    out = tmp_path / "linker.jsonl"
    n = write_pairs_jsonl(records, out)
    lines = out.read_text(encoding="utf-8").splitlines()
    assert n == len(lines)
    parsed = [json.loads(line) for line in lines]
    assert parsed == records
