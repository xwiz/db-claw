"""Fixture-graph builder used by every generator + ONNX-export test.

Builds a tiny SemanticGraph with two entities (``users``, ``tenants``) and
enough fields to exercise the generator's filter / aggregate / enum paths.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


def build(path: Path) -> Path:
    """Write a fresh `.semsql` fixture to ``path`` and return ``path``."""
    if path.exists():
        path.unlink()
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
            CREATE TABLE fields (
                entity           TEXT NOT NULL,
                field            TEXT NOT NULL,
                db_column        TEXT NOT NULL,
                type             TEXT NOT NULL,
                display_label    TEXT,
                enum_canonical   TEXT,
                unit_canonical   TEXT,
                proto_blob       BLOB NOT NULL,
                PRIMARY KEY (entity, field)
            );
            CREATE TABLE enums (
                canonical_name        TEXT PRIMARY KEY,
                _enum_values_json     TEXT NOT NULL DEFAULT '{}'
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
            "INSERT INTO semsql_metadata (key, value) VALUES ('schema_version', '1')"
        )
        # Entities
        conn.execute(
            "INSERT INTO entities VALUES ('users', 'users', 'public', 'Student', 'Students', X'')"
        )
        conn.execute(
            "INSERT INTO entities VALUES "
            "('tenants', 'tenants', 'public', 'Organization', 'Organizations', X'')"
        )
        # Fields on users
        for entity, name, col, ty, label, enum_can in [
            ("users", "id", "id", "bigint", "ID", None),
            ("users", "name", "full_name", "string", "Name", None),
            ("users", "balance", "balance_cents", "integer", "Balance", None),
            ("users", "created_at", "created_at", "timestamp", "Joined Date", None),
            ("users", "status_code", "status_code", "integer", "Status", "users.status_code"),
            ("users", "tenant_id", "tenant_id", "bigint", "Organization", None),
        ]:
            conn.execute(
                "INSERT INTO fields VALUES (?,?,?,?,?,?,NULL,X'')",
                (entity, name, col, ty, label, enum_can),
            )
        # Fields on tenants — shared name `created_at` makes a hard negative.
        for entity, name, col, ty, label, enum_can in [
            ("tenants", "id", "id", "bigint", "ID", None),
            ("tenants", "name", "name", "string", "Name", None),
            ("tenants", "created_at", "created_at", "timestamp", "Founded At", None),
        ]:
            conn.execute(
                "INSERT INTO fields VALUES (?,?,?,?,?,?,NULL,X'')",
                (entity, name, col, ty, label, enum_can),
            )
        # Enum
        conn.execute(
            "INSERT INTO enums (canonical_name, _enum_values_json) VALUES (?,?)",
            ("users.status_code", json.dumps({"1": "Pending", "2": "Active", "39": "Error"})),
        )
        # Scope (used by the round-trip injector test elsewhere)
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
        conn.commit()
    finally:
        conn.close()
    return path
