"""SemanticGraph reader — load ScopeRule instances from a `.semsql` file."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from semsql_rewriter.graph_reader import (
    SUPPORTED_SCHEMA_VERSION,
    GraphReadError,
    load_scope_rules,
    schema_version,
)


def _make_graph(path: Path, *, version: int = SUPPORTED_SCHEMA_VERSION) -> None:
    """Build a minimal valid `.semsql` file with one entity + scope row.

    Mirrors the schema in ``crates/semsql-graph/src/lib.rs::SCHEMA_V1_SQL``.
    """
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE semsql_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE entities (
                canonical_name   TEXT PRIMARY KEY,
                db_table         TEXT NOT NULL,
                db_schema        TEXT,
                singular_label   TEXT,
                plural_label     TEXT,
                proto_blob       BLOB NOT NULL
            );
            CREATE TABLE scopes (
                entity           TEXT NOT NULL,
                kind             TEXT NOT NULL,
                template         TEXT NOT NULL,
                required_params  TEXT NOT NULL,
                source_rule      TEXT,
                PRIMARY KEY (entity, kind, template)
            );
            """
        )
        conn.execute(
            "INSERT INTO semsql_metadata (key, value) VALUES ('schema_version', ?)",
            (str(version),),
        )
        conn.execute(
            "INSERT INTO entities VALUES ('users', 'users', 'public', 'Student', 'Students', X'')"
        )
        conn.execute(
            "INSERT INTO scopes VALUES (?, ?, ?, ?, ?)",
            (
                "users",
                "tenant",
                "{{table}}.tenant_id = :tenant",
                json.dumps(["tenant"]),
                "tenant_isolation",
            ),
        )
        conn.execute(
            "INSERT INTO scopes VALUES (?, ?, ?, ?, ?)",
            (
                "users",
                "soft_delete",
                "{{table}}.deleted_at IS NULL",
                json.dumps([]),
                "soft_delete",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_round_trip_loads_two_scopes_conjoined(tmp_path: Path) -> None:
    graph = tmp_path / "test.semsql"
    _make_graph(graph)
    rules = load_scope_rules(graph)
    assert "users" in rules
    rule = rules["users"]
    assert "tenant_id = :tenant" in rule.template
    assert "deleted_at IS NULL" in rule.template
    assert rule.required_params == ("tenant",)


def test_schema_version_too_new_rejected(tmp_path: Path) -> None:
    graph = tmp_path / "test.semsql"
    _make_graph(graph, version=SUPPORTED_SCHEMA_VERSION + 1)
    with pytest.raises(GraphReadError):
        load_scope_rules(graph)


def test_schema_version_function(tmp_path: Path) -> None:
    graph = tmp_path / "test.semsql"
    _make_graph(graph)
    assert schema_version(graph) == SUPPORTED_SCHEMA_VERSION


def test_missing_template_placeholder_rejected(tmp_path: Path) -> None:
    graph = tmp_path / "test.semsql"
    _make_graph(graph)
    # Corrupt the template — missing {{table}}.
    conn = sqlite3.connect(graph)
    conn.execute(
        "UPDATE scopes SET template = 'tenant_id = :tenant' "
        "WHERE entity = 'users' AND kind = 'tenant'"
    )
    conn.commit()
    conn.close()
    with pytest.raises(GraphReadError):
        load_scope_rules(graph)


def test_round_trip_with_real_injector(tmp_path: Path) -> None:
    """End-to-end: read graph → apply scope → assert query is scoped."""
    from semsql_rewriter.injector import inject

    graph = tmp_path / "test.semsql"
    _make_graph(graph)
    rules = load_scope_rules(graph)
    result = inject("SELECT * FROM users", rules, {"tenant": "42"}, dialect="sqlite")
    sql = result.sql.lower()
    assert "tenant_id = :tenant" in sql
    assert "deleted_at is null" in sql
