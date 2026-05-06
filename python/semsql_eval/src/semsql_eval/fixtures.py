"""Spider 1.0 mini-corpus fixture builder.

The real Spider 1.0 ships as a ~1 GB tarball with 200 databases; CI
can't download it on every run, and developers shouldn't have to to
smoke-test the cascade end-to-end. This module builds a synthetic
Spider-shaped layout from in-memory definitions:

    <out>/dev.json          # eval manifest (compatible with SpiderSuite.load)
    <out>/tables.json       # schema dump in Spider's format
    <out>/database/
        <db_id>/
            <db_id>.sqlite

The corpus covers the most-needed cascade behaviours:

  - Stage 0a: trivial `show <entity>` queries that the pre-resolver
    handles deterministically, exec_acc → 100% on these.
  - Stage 0b: intent-library queries ("top 5 customers in the red")
    that fire pattern matches.
  - Stage 1+ holdout: queries that require model inference (cascade
    bails to sentinel without weights, so they land in the `bailed`
    bucket — exactly what the user wants to see in their report).

Public entry points:

  - :func:`build_corpus` — write a fresh corpus to a directory.
  - :func:`MINI_CORPUS` — the canonical in-memory spec; reusable in
    pytest fixtures.

The corpus is *not* a substitute for real Spider eval; it's a smoke
test that the harness wiring works end-to-end.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

__all__ = [
    "ColumnSpec",
    "TableSpec",
    "DbSpec",
    "ExampleSpec",
    "MiniCorpus",
    "MINI_CORPUS",
    "build_corpus",
]


@dataclass(frozen=True)
class ColumnSpec:
    """One column declaration in the synthetic schema."""

    name: str
    sql_type: str
    """e.g. ``"INTEGER"``, ``"TEXT"``, ``"REAL"``."""


@dataclass(frozen=True)
class TableSpec:
    """One table declaration. Rows are tuples — order matches columns."""

    name: str
    columns: tuple[ColumnSpec, ...]
    rows: tuple[tuple[object, ...], ...] = ()


@dataclass(frozen=True)
class DbSpec:
    """One synthetic database — a Spider `db_id` plus its tables."""

    db_id: str
    tables: tuple[TableSpec, ...]


@dataclass(frozen=True)
class ExampleSpec:
    """One eval example. ``gold_sql`` MUST be deterministic given the
    fixture's row contents — random or non-deterministic SQL would
    make the smoke test flaky."""

    db_id: str
    question: str
    gold_sql: str


@dataclass(frozen=True)
class MiniCorpus:
    """Aggregate spec — written to disk by :func:`build_corpus`."""

    dbs: tuple[DbSpec, ...]
    examples: tuple[ExampleSpec, ...]


# ---------------------------------------------------------------------------
# canonical mini corpus
# ---------------------------------------------------------------------------

# Three small DBs with deliberately distinct schema shapes. Numbers
# stay small so SQL on the test DBs is fast and exec-equality is
# robust to deterministic ordering.

_TENANT_DB = DbSpec(
    db_id="tenant_app",
    tables=(
        TableSpec(
            name="users",
            columns=(
                ColumnSpec("id", "INTEGER"),
                ColumnSpec("email", "TEXT"),
                ColumnSpec("tenant_id", "INTEGER"),
                ColumnSpec("status", "TEXT"),
            ),
            rows=(
                (1, "alice@a.io", 1, "active"),
                (2, "bob@a.io", 1, "archived"),
                (3, "carol@b.io", 2, "active"),
            ),
        ),
        TableSpec(
            name="tenants",
            columns=(
                ColumnSpec("id", "INTEGER"),
                ColumnSpec("name", "TEXT"),
                ColumnSpec("plan", "TEXT"),
            ),
            rows=(
                (1, "Acme", "pro"),
                (2, "Globex", "free"),
            ),
        ),
    ),
)

_FINANCE_DB = DbSpec(
    db_id="finance",
    tables=(
        TableSpec(
            name="invoices",
            columns=(
                ColumnSpec("id", "INTEGER"),
                ColumnSpec("customer_id", "INTEGER"),
                ColumnSpec("amount", "REAL"),
                ColumnSpec("status", "TEXT"),
            ),
            rows=(
                (1, 100, 99.99, "paid"),
                (2, 100, 250.0, "overdue"),
                (3, 200, 5000.0, "paid"),
            ),
        ),
        TableSpec(
            name="customers",
            columns=(
                ColumnSpec("id", "INTEGER"),
                ColumnSpec("name", "TEXT"),
                ColumnSpec("country", "TEXT"),
            ),
            rows=(
                (100, "Yoyodyne", "US"),
                (200, "OmniCorp", "DE"),
            ),
        ),
    ),
)

_BLOG_DB = DbSpec(
    db_id="blog",
    tables=(
        TableSpec(
            name="posts",
            columns=(
                ColumnSpec("id", "INTEGER"),
                ColumnSpec("title", "TEXT"),
                ColumnSpec("author", "TEXT"),
                ColumnSpec("published", "INTEGER"),
            ),
            rows=(
                (1, "First post", "alice", 1),
                (2, "Draft", "alice", 0),
                (3, "Hello", "bob", 1),
            ),
        ),
    ),
)

_EXAMPLES = (
    # Stage 0a candidates — trivial `show <entity>` style. The
    # cascade's pre-resolver handles these.
    ExampleSpec(
        db_id="tenant_app",
        question="show tenants",
        gold_sql="SELECT * FROM tenants",
    ),
    ExampleSpec(
        db_id="blog",
        question="show posts",
        gold_sql="SELECT * FROM posts",
    ),
    # Stage 0b candidates — intent-library idioms (currently bail
    # without trained weights, but the question text fires an intent
    # match).
    ExampleSpec(
        db_id="finance",
        question="top 1 customer in the red",
        gold_sql=(
            "SELECT customers.name FROM customers "
            "JOIN invoices ON invoices.customer_id = customers.id "
            "WHERE invoices.status = 'overdue' "
            "ORDER BY invoices.amount DESC LIMIT 1"
        ),
    ),
    # Stage 1+ holdout — non-trivial join + filter; cascade bails
    # without weights.
    ExampleSpec(
        db_id="tenant_app",
        question="archived users in the Acme tenant",
        gold_sql=(
            "SELECT users.email FROM users "
            "JOIN tenants ON tenants.id = users.tenant_id "
            "WHERE users.status = 'archived' AND tenants.name = 'Acme'"
        ),
    ),
    ExampleSpec(
        db_id="blog",
        question="published posts by alice",
        gold_sql=(
            "SELECT title FROM posts "
            "WHERE published = 1 AND author = 'alice'"
        ),
    ),
)


MINI_CORPUS = MiniCorpus(
    dbs=(_TENANT_DB, _FINANCE_DB, _BLOG_DB),
    examples=_EXAMPLES,
)


# ---------------------------------------------------------------------------
# disk writer
# ---------------------------------------------------------------------------


def build_corpus(out: Path, corpus: MiniCorpus = MINI_CORPUS) -> Path:
    """Materialise the corpus on disk under ``out``. Returns ``out``.

    Idempotent: each existing `.sqlite` file is unlinked + recreated
    from scratch on every call so row contents match the spec
    exactly. Manifest files (`dev.json`, `tables.json`) are
    overwritten in place. The caller can additionally wipe ``out``
    if they want a fully clean slate (the writer never sweeps stale
    db_ids that were dropped from the spec — that's a manual step).
    """
    out.mkdir(parents=True, exist_ok=True)
    db_root = out / "database"
    db_root.mkdir(exist_ok=True)

    for db in corpus.dbs:
        db_dir = db_root / db.db_id
        db_dir.mkdir(exist_ok=True)
        sqlite_path = db_dir / f"{db.db_id}.sqlite"
        _write_sqlite(sqlite_path, db.tables)

    dev_json = [
        {
            "db_id": ex.db_id,
            "question": ex.question,
            "query": ex.gold_sql,
        }
        for ex in corpus.examples
    ]
    (out / "dev.json").write_text(
        json.dumps(dev_json, indent=2) + "\n", encoding="utf-8"
    )

    tables_json = [_db_to_spider_tables(db) for db in corpus.dbs]
    (out / "tables.json").write_text(
        json.dumps(tables_json, indent=2) + "\n", encoding="utf-8"
    )

    return out


def _write_sqlite(path: Path, tables: Sequence[TableSpec]) -> None:
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    try:
        for t in tables:
            cols_sql = ", ".join(f"{c.name} {c.sql_type}" for c in t.columns)
            conn.execute(f"CREATE TABLE {t.name} ({cols_sql})")
            if t.rows:
                placeholders = ", ".join("?" for _ in t.columns)
                conn.executemany(
                    f"INSERT INTO {t.name} VALUES ({placeholders})",
                    [tuple(row) for row in t.rows],
                )
        conn.commit()
    finally:
        conn.close()


def _db_to_spider_tables(db: DbSpec) -> dict[str, object]:
    """Render one DbSpec as a single entry in Spider's `tables.json`.

    Spider's format is positional — `column_names` is a list of
    `[table_idx, name]` tuples and `column_types` is a parallel list
    of SQL types. We populate enough fields that downstream parsers
    don't crash; the cascade only consumes `db_id` + per-DB SQLite
    introspection, so the schema dump is largely informational.
    """
    table_names = [t.name for t in db.tables]
    column_names: list[list[object]] = [[-1, "*"]]
    column_types: list[str] = ["text"]
    for ti, t in enumerate(db.tables):
        for c in t.columns:
            column_names.append([ti, c.name])
            column_types.append(_spider_type(c.sql_type))
    return {
        "db_id": db.db_id,
        "table_names_original": table_names,
        "table_names": table_names,
        "column_names_original": column_names,
        "column_names": column_names,
        "column_types": column_types,
        "primary_keys": [],
        "foreign_keys": [],
    }


def _spider_type(sql_type: str) -> str:
    sql_upper = sql_type.upper()
    if "INT" in sql_upper:
        return "number"
    if "REAL" in sql_upper or "FLOAT" in sql_upper or "DOUBLE" in sql_upper:
        return "number"
    return "text"
