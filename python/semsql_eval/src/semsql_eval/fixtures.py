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
import random
import sqlite3
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

__all__ = [
    "MINI_CORPUS",
    "CanaryRejectCase",
    "CanaryRoutedCase",
    "ColumnSpec",
    "DbSpec",
    "ExampleSpec",
    "ForeignKeySpec",
    "MiniCorpus",
    "TableSpec",
    "build_corpus",
    "build_queryframe_canary",
    "write_queryframe_canary_mysql_sql",
    "write_queryframe_canary_postgres_sql",
]


@dataclass(frozen=True)
class ColumnSpec:
    """One column declaration in the synthetic schema."""

    name: str
    sql_type: str
    """e.g. ``"INTEGER"``, ``"TEXT"``, ``"REAL"``."""


@dataclass(frozen=True)
class ForeignKeySpec:
    """One SQLite foreign-key declaration on a table."""

    column: str
    ref_table: str
    ref_column: str


@dataclass(frozen=True)
class TableSpec:
    """One table declaration. Rows are tuples — order matches columns."""

    name: str
    columns: tuple[ColumnSpec, ...]
    rows: tuple[tuple[object, ...], ...] = ()
    primary_key: tuple[str, ...] = ()
    foreign_keys: tuple[ForeignKeySpec, ...] = ()


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


@dataclass(frozen=True)
class CanaryRoutedCase:
    """One executable QueryFrame production canary case."""

    db_id: str
    question: str
    gold_sql: str
    kind: str


@dataclass(frozen=True)
class CanaryRejectCase:
    """One canary prompt that should clarify instead of routing."""

    db_id: str
    question: str
    kind: str
    reason: str


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
# seeded QueryFrame production canary
# ---------------------------------------------------------------------------


_QUERYFRAME_CANARY_VARIANTS = frozenset({"commerce", "alias", "random_alias"})


@dataclass(frozen=True)
class _CanaryNamingPlan:
    db_id: str
    region_table: str
    account_table: str
    product_table: str
    order_table: str
    account_term: str
    product_term: str
    order_term: str
    order_singular: str
    account_fk: str
    product_fk: str
    region_fk: str
    account_name_field: str
    order_date_field: str


def build_queryframe_canary(
    out: Path, seed: int = 20260601, variant: str = "commerce"
) -> Path:
    """Materialise a seeded SQLite canary for production hardening.

    The mini-corpus above proves eval wiring. This canary is different: it
    keeps one compact commerce-style graph, varies business values by seed,
    optionally varies table/field names, includes real SQLite foreign keys,
    and writes a sidecar with routed cases plus prompts that should clarify
    rather than route.
    """
    if variant not in _QUERYFRAME_CANARY_VARIANTS:
        valid = ", ".join(sorted(_QUERYFRAME_CANARY_VARIANTS))
        raise ValueError(f"unknown QueryFrame canary variant {variant!r}; expected {valid}")
    corpus, routed_cases, reject_cases, naming_plan = _queryframe_canary_spec(
        seed, variant=variant
    )
    build_corpus(out, corpus)
    sidecar = {
        "schema_version": 1,
        "seed": seed,
        "variant": variant,
        "naming_plan": asdict(naming_plan),
        "purpose": "queryframe_state_machine_production_canary",
        "routed_cases": [
            {
                "db_id": case.db_id,
                "question": case.question,
                "query": case.gold_sql,
                "kind": case.kind,
                "should_route": True,
            }
            for case in routed_cases
        ],
        "reject_cases": [
            {
                "db_id": case.db_id,
                "question": case.question,
                "kind": case.kind,
                "reason": case.reason,
                "should_route": False,
            }
            for case in reject_cases
        ],
    }
    (out / "queryframe_canary.json").write_text(
        json.dumps(sidecar, indent=2) + "\n", encoding="utf-8"
    )
    return out


def write_queryframe_canary_postgres_sql(
    out: Path,
    *,
    seed: int = 20260601,
    variant: str = "commerce",
    schema: str = "semsql_queryframe_canary",
) -> dict[str, Path]:
    """Write Postgres setup/teardown SQL for the seeded canary.

    The SQLite canary is the always-on execution surface. This companion
    emits the same schema/data for a live Postgres database so extractor
    parity can be gated when a throwaway Postgres URL is available.
    """
    if variant not in _QUERYFRAME_CANARY_VARIANTS:
        valid = ", ".join(sorted(_QUERYFRAME_CANARY_VARIANTS))
        raise ValueError(f"unknown QueryFrame canary variant {variant!r}; expected {valid}")
    corpus, _routed_cases, _reject_cases, _naming_plan = _queryframe_canary_spec(
        seed, variant=variant
    )
    if len(corpus.dbs) != 1:
        raise ValueError("queryframe canary must contain exactly one database")
    pg_dir = out / "postgres"
    pg_dir.mkdir(parents=True, exist_ok=True)
    setup_path = pg_dir / "setup.sql"
    teardown_path = pg_dir / "teardown.sql"
    setup_path.write_text(
        _postgres_setup_sql(corpus.dbs[0], schema=schema),
        encoding="utf-8",
    )
    teardown_path.write_text(
        f"DROP SCHEMA IF EXISTS {_quote_pg_ident(schema)} CASCADE;\n",
        encoding="utf-8",
    )
    return {"setup": setup_path, "teardown": teardown_path}


def write_queryframe_canary_mysql_sql(
    out: Path,
    *,
    seed: int = 20260601,
    variant: str = "commerce",
    database: str = "semsql_queryframe_canary",
) -> dict[str, Path]:
    """Write MySQL/MariaDB setup/teardown SQL for the seeded canary."""
    if variant not in _QUERYFRAME_CANARY_VARIANTS:
        valid = ", ".join(sorted(_QUERYFRAME_CANARY_VARIANTS))
        raise ValueError(f"unknown QueryFrame canary variant {variant!r}; expected {valid}")
    corpus, _routed_cases, _reject_cases, _naming_plan = _queryframe_canary_spec(
        seed, variant=variant
    )
    if len(corpus.dbs) != 1:
        raise ValueError("queryframe canary must contain exactly one database")
    mysql_dir = out / "mysql"
    mysql_dir.mkdir(parents=True, exist_ok=True)
    setup_path = mysql_dir / "setup.sql"
    teardown_path = mysql_dir / "teardown.sql"
    setup_path.write_text(
        _mysql_setup_sql(corpus.dbs[0], database=database),
        encoding="utf-8",
    )
    teardown_path.write_text(
        f"DROP DATABASE IF EXISTS {_quote_mysql_ident(database)};\n",
        encoding="utf-8",
    )
    return {"setup": setup_path, "teardown": teardown_path}


def _queryframe_canary_spec(
    seed: int, *, variant: str,
) -> tuple[
    MiniCorpus,
    tuple[CanaryRoutedCase, ...],
    tuple[CanaryRejectCase, ...],
    _CanaryNamingPlan,
]:
    rng = random.Random(seed)
    suffix = rng.randrange(1000, 9999)
    zip_a = f"{rng.randrange(10000, 99999)}-{rng.randrange(1000, 9999)}"
    zip_b = f"{rng.randrange(10000, 99999)}-{rng.randrange(1000, 9999)}"
    order_day = rng.randrange(10, 25)
    order_date = f"2024-02-{order_day:02d}"
    other_date = f"2024-03-{rng.randrange(1, 25):02d}"
    regions = ("EMEA", "APAC", "LATAM")
    focus_region = regions[rng.randrange(0, len(regions))]
    other_regions = tuple(region for region in regions if region != focus_region)
    acme_name = f"Acme {suffix}"
    orbit_name = f"Orbit {rng.randrange(100, 999)}"
    northstar_name = f"Northstar {rng.randrange(100, 999)}"
    atlas_product = f"Atlas {rng.randrange(100, 999)}"
    beacon_product = f"Beacon {rng.randrange(100, 999)}"
    naming = _queryframe_canary_naming_plan(variant, rng)

    db = DbSpec(
        db_id=naming.db_id,
        tables=(
            TableSpec(
                name=naming.region_table,
                columns=(
                    ColumnSpec("id", "INTEGER"),
                    ColumnSpec("name", "TEXT"),
                ),
                rows=(
                    (1, focus_region),
                    (2, other_regions[0]),
                    (3, other_regions[1]),
                ),
                primary_key=("id",),
            ),
            TableSpec(
                name=naming.account_table,
                columns=(
                    ColumnSpec("id", "INTEGER"),
                    ColumnSpec(naming.account_name_field, "TEXT"),
                    ColumnSpec("status", "TEXT"),
                    ColumnSpec(naming.region_fk, "INTEGER"),
                    ColumnSpec("external_code", "TEXT"),
                    ColumnSpec("joined_on", "TEXT"),
                ),
                rows=(
                    (1, acme_name, "active", 1, "00D4", "2000/1/1"),
                    (2, orbit_name, "paused", 2, "0613360", "2022/5/15"),
                    (3, northstar_name, "active", 1, f"ID-{suffix}", "2023/9/30"),
                ),
                primary_key=("id",),
                foreign_keys=(
                    ForeignKeySpec(naming.region_fk, naming.region_table, "id"),
                ),
            ),
            TableSpec(
                name=naming.product_table,
                columns=(
                    ColumnSpec("id", "INTEGER"),
                    ColumnSpec("name", "TEXT"),
                    ColumnSpec("category", "TEXT"),
                ),
                rows=(
                    (10, atlas_product, "analytics"),
                    (11, beacon_product, "support"),
                ),
                primary_key=("id",),
            ),
            TableSpec(
                name=naming.order_table,
                columns=(
                    ColumnSpec("id", "INTEGER"),
                    ColumnSpec(naming.account_fk, "INTEGER"),
                    ColumnSpec(naming.product_fk, "INTEGER"),
                    ColumnSpec("amount", "REAL"),
                    ColumnSpec(naming.order_date_field, "TEXT"),
                    ColumnSpec("zip_code", "TEXT"),
                ),
                rows=(
                    (100, 1, 10, 120.50, order_date, zip_a),
                    (101, 1, 11, 75.25, other_date, zip_b),
                    (102, 3, 10, 200.00, order_date, zip_a),
                ),
                primary_key=("id",),
                foreign_keys=(
                    ForeignKeySpec(naming.account_fk, naming.account_table, "id"),
                    ForeignKeySpec(naming.product_fk, naming.product_table, "id"),
                ),
            ),
        ),
    )

    routed_cases = (
        CanaryRoutedCase(
            db_id=naming.db_id,
            question=f"list active {naming.account_term}",
            gold_sql=(
                f"SELECT {naming.account_table}.{naming.account_name_field} "
                f"FROM {naming.account_table} "
                f"WHERE {naming.account_table}.status = 'active' "
                f"ORDER BY {naming.account_table}.{naming.account_name_field}"
            ),
            kind="enum_filter",
        ),
        CanaryRoutedCase(
            db_id=naming.db_id,
            question=f"show active {naming.account_term}",
            gold_sql=(
                f"SELECT {naming.account_table}.{naming.account_name_field} "
                f"FROM {naming.account_table} "
                f"WHERE {naming.account_table}.status = 'active' "
                f"ORDER BY {naming.account_table}.{naming.account_name_field}"
            ),
            kind="paraphrase_enum_filter",
        ),
        CanaryRoutedCase(
            db_id=naming.db_id,
            question=f"which {naming.account_term} are active",
            gold_sql=(
                f"SELECT {naming.account_table}.{naming.account_name_field} "
                f"FROM {naming.account_table} "
                f"WHERE {naming.account_table}.status = 'active' "
                f"ORDER BY {naming.account_table}.{naming.account_name_field}"
            ),
            kind="paraphrase_enum_filter",
        ),
        CanaryRoutedCase(
            db_id=naming.db_id,
            question=f"how many active {naming.account_term} are in {focus_region}",
            gold_sql=(
                f"SELECT COUNT({naming.account_table}.id) "
                f"FROM {naming.account_table} "
                f"JOIN {naming.region_table} "
                f"ON {naming.account_table}.{naming.region_fk} = {naming.region_table}.id "
                f"WHERE {naming.account_table}.status = 'active' "
                f"AND {naming.region_table}.name = '{focus_region}'"
            ),
            kind="join_count_filter",
        ),
        CanaryRoutedCase(
            db_id=naming.db_id,
            question=f"count active {naming.account_term} in {focus_region}",
            gold_sql=(
                f"SELECT COUNT({naming.account_table}.id) "
                f"FROM {naming.account_table} "
                f"JOIN {naming.region_table} "
                f"ON {naming.account_table}.{naming.region_fk} = {naming.region_table}.id "
                f"WHERE {naming.account_table}.status = 'active' "
                f"AND {naming.region_table}.name = '{focus_region}'"
            ),
            kind="paraphrase_join_count_filter",
        ),
        CanaryRoutedCase(
            db_id=naming.db_id,
            question=f"total {naming.order_singular} amount for {acme_name}",
            gold_sql=(
                f"SELECT SUM({naming.order_table}.amount) FROM {naming.order_table} "
                f"JOIN {naming.account_table} "
                f"ON {naming.order_table}.{naming.account_fk} = {naming.account_table}.id "
                f"WHERE {naming.account_table}.{naming.account_name_field} = '{acme_name}'"
            ),
            kind="join_value_aggregate",
        ),
        CanaryRoutedCase(
            db_id=naming.db_id,
            question=f"sum of {naming.order_term} for {acme_name}",
            gold_sql=(
                f"SELECT SUM({naming.order_table}.amount) FROM {naming.order_table} "
                f"JOIN {naming.account_table} "
                f"ON {naming.order_table}.{naming.account_fk} = {naming.account_table}.id "
                f"WHERE {naming.account_table}.{naming.account_name_field} = '{acme_name}'"
            ),
            kind="paraphrase_join_value_aggregate",
        ),
        CanaryRoutedCase(
            db_id=naming.db_id,
            question=(
                f"average {naming.order_singular} amount for "
                f"{naming.account_term} in {focus_region}"
            ),
            gold_sql=(
                f"SELECT AVG({naming.order_table}.amount) FROM {naming.order_table} "
                f"JOIN {naming.account_table} "
                f"ON {naming.order_table}.{naming.account_fk} = {naming.account_table}.id "
                f"JOIN {naming.region_table} "
                f"ON {naming.account_table}.{naming.region_fk} = {naming.region_table}.id "
                f"WHERE {naming.region_table}.name = '{focus_region}'"
            ),
            kind="join_aggregate",
        ),
        CanaryRoutedCase(
            db_id=naming.db_id,
            question=(
                f"avg {naming.order_singular} amount for "
                f"{focus_region} {naming.account_term}"
            ),
            gold_sql=(
                f"SELECT AVG({naming.order_table}.amount) FROM {naming.order_table} "
                f"JOIN {naming.account_table} "
                f"ON {naming.order_table}.{naming.account_fk} = {naming.account_table}.id "
                f"JOIN {naming.region_table} "
                f"ON {naming.account_table}.{naming.region_fk} = {naming.region_table}.id "
                f"WHERE {naming.region_table}.name = '{focus_region}'"
            ),
            kind="paraphrase_join_aggregate",
        ),
        CanaryRoutedCase(
            db_id=naming.db_id,
            question=f"top 2 {naming.product_term} by {naming.order_singular} amount",
            gold_sql=(
                f"SELECT {naming.product_table}.name, "
                f"SUM({naming.order_table}.amount) AS total_amount "
                f"FROM {naming.product_table} "
                f"JOIN {naming.order_table} "
                f"ON {naming.order_table}.{naming.product_fk} = {naming.product_table}.id "
                f"GROUP BY {naming.product_table}.name "
                "ORDER BY total_amount DESC LIMIT 2"
            ),
            kind="topk_group_aggregate",
        ),
        CanaryRoutedCase(
            db_id=naming.db_id,
            question=(
                f"which 2 {naming.product_term} have the highest "
                f"{naming.order_singular} amount"
            ),
            gold_sql=(
                f"SELECT {naming.product_table}.name, "
                f"SUM({naming.order_table}.amount) AS total_amount "
                f"FROM {naming.product_table} "
                f"JOIN {naming.order_table} "
                f"ON {naming.order_table}.{naming.product_fk} = {naming.product_table}.id "
                f"GROUP BY {naming.product_table}.name "
                "ORDER BY total_amount DESC LIMIT 2"
            ),
            kind="paraphrase_topk_group_aggregate",
        ),
        CanaryRoutedCase(
            db_id=naming.db_id,
            question=f"{naming.order_term} shipped to ZIP {zip_a}",
            gold_sql=(
                f"SELECT {naming.order_table}.id FROM {naming.order_table} "
                f"WHERE {naming.order_table}.zip_code = '{zip_a}'"
            ),
            kind="structured_literal_zip",
        ),
        CanaryRoutedCase(
            db_id=naming.db_id,
            question=f"{naming.order_term} shipped to {zip_a}",
            gold_sql=(
                f"SELECT {naming.order_table}.id FROM {naming.order_table} "
                f"WHERE {naming.order_table}.zip_code = '{zip_a}'"
            ),
            kind="paraphrase_structured_literal_zip",
        ),
        CanaryRoutedCase(
            db_id=naming.db_id,
            question=f"{_singularize_term(naming.account_term)} with external code 00D4",
            gold_sql=(
                f"SELECT {naming.account_table}.{naming.account_name_field} "
                f"FROM {naming.account_table} "
                f"WHERE {naming.account_table}.external_code = '00D4'"
            ),
            kind="structured_literal_code",
        ),
        CanaryRoutedCase(
            db_id=naming.db_id,
            question="who has external code 00D4",
            gold_sql=(
                f"SELECT {naming.account_table}.{naming.account_name_field} "
                f"FROM {naming.account_table} "
                f"WHERE {naming.account_table}.external_code = '00D4'"
            ),
            kind="paraphrase_structured_literal_code",
        ),
        CanaryRoutedCase(
            db_id=naming.db_id,
            question=f"{naming.order_term} placed on {order_date}",
            gold_sql=(
                f"SELECT {naming.order_table}.id FROM {naming.order_table} "
                f"WHERE {naming.order_table}.{naming.order_date_field} = '{order_date}'"
            ),
            kind="structured_literal_date",
        ),
    )
    reject_cases = (
        CanaryRejectCase(
            db_id=naming.db_id,
            question="show status",
            kind="ambiguous_column",
            reason="status is a dimension column, not a routeable entity by itself",
        ),
        CanaryRejectCase(
            db_id=naming.db_id,
            question="average amount by name",
            kind="underspecified_metric_dimension",
            reason="name could refer to accounts, products, or regions",
        ),
    )
    corpus = MiniCorpus(
        dbs=(db,),
        examples=tuple(
            ExampleSpec(case.db_id, case.question, case.gold_sql)
            for case in routed_cases
        ),
    )
    return corpus, routed_cases, reject_cases, naming


def _queryframe_canary_naming_plan(
    variant: str, rng: random.Random
) -> _CanaryNamingPlan:
    if variant == "commerce":
        return _CanaryNamingPlan(
            db_id="commerce_canary",
            region_table="regions",
            account_table="accounts",
            product_table="products",
            order_table="orders",
            account_term="accounts",
            product_term="products",
            order_term="orders",
            order_singular="order",
            account_fk="account_id",
            product_fk="product_id",
            region_fk="region_id",
            account_name_field="company_name",
            order_date_field="ordered_on",
        )
    if variant == "alias":
        return _CanaryNamingPlan(
            db_id="commerce_alias_canary",
            region_table="territories",
            account_table="clients",
            product_table="catalog_items",
            order_table="transactions",
            account_term="clients",
            product_term="items",
            order_term="transactions",
            order_singular="transaction",
            account_fk="client_id",
            product_fk="item_id",
            region_fk="territory_id",
            account_name_field="company_name",
            order_date_field="ordered_on",
        )
    account_table, account_term, account_fk, account_name_field = rng.choice(
        (
            ("clients", "clients", "client_id", "company_name"),
            ("customers", "customers", "customer_id", "company_name"),
            ("businesses", "businesses", "business_id", "business_name"),
            (
                "organizations",
                "organizations",
                "organization_id",
                "organization_name",
            ),
        )
    )
    region_table, region_fk = rng.choice(
        (
            ("territories", "territory_id"),
            ("markets", "market_id"),
            ("zones", "zone_id"),
        )
    )
    product_table, product_term, product_fk = rng.choice(
        (
            ("catalog_items", "items", "item_id"),
            ("services", "services", "service_id"),
            ("offerings", "offerings", "offering_id"),
        )
    )
    order_table, order_term, order_singular, order_date_field = rng.choice(
        (
            ("transactions", "transactions", "transaction", "transaction_date"),
            ("sales", "sales", "sale", "sale_date"),
            ("purchases", "purchases", "purchase", "purchase_date"),
        )
    )
    return _CanaryNamingPlan(
        db_id="commerce_random_alias_canary",
        region_table=region_table,
        account_table=account_table,
        product_table=product_table,
        order_table=order_table,
        account_term=account_term,
        product_term=product_term,
        order_term=order_term,
        order_singular=order_singular,
        account_fk=account_fk,
        product_fk=product_fk,
        region_fk=region_fk,
        account_name_field=account_name_field,
        order_date_field=order_date_field,
    )


def _singularize_term(term: str) -> str:
    if term.endswith("ies"):
        return f"{term[:-3]}y"
    if term.endswith("ses"):
        return term[:-2]
    if term.endswith("s"):
        return term[:-1]
    return term


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
        conn.execute("PRAGMA foreign_keys = ON")
        for t in tables:
            declarations = [
                f"{_quote_ident(c.name)} {c.sql_type}" for c in t.columns
            ]
            if t.primary_key:
                cols = ", ".join(_quote_ident(c) for c in t.primary_key)
                declarations.append(f"PRIMARY KEY ({cols})")
            for fk in t.foreign_keys:
                declarations.append(
                    "FOREIGN KEY "
                    f"({_quote_ident(fk.column)}) REFERENCES "
                    f"{_quote_ident(fk.ref_table)}({_quote_ident(fk.ref_column)})"
                )
            cols_sql = ", ".join(declarations)
            conn.execute(f"CREATE TABLE {_quote_ident(t.name)} ({cols_sql})")
            if t.rows:
                placeholders = ", ".join("?" for _ in t.columns)
                conn.executemany(
                    f"INSERT INTO {_quote_ident(t.name)} VALUES ({placeholders})",
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
    column_index: dict[tuple[str, str], int] = {}
    primary_keys: list[int] = []
    foreign_keys: list[list[int]] = []
    for ti, t in enumerate(db.tables):
        for c in t.columns:
            column_index[(t.name, c.name)] = len(column_names)
            column_names.append([ti, c.name])
            column_types.append(_spider_type(c.sql_type))
        for pk_column in t.primary_key:
            primary_keys.append(column_index[(t.name, pk_column)])
    for t in db.tables:
        for fk in t.foreign_keys:
            foreign_keys.append(
                [
                    column_index[(t.name, fk.column)],
                    column_index[(fk.ref_table, fk.ref_column)],
                ]
            )
    return {
        "db_id": db.db_id,
        "table_names_original": table_names,
        "table_names": table_names,
        "column_names_original": column_names,
        "column_names": column_names,
        "column_types": column_types,
        "primary_keys": primary_keys,
        "foreign_keys": foreign_keys,
    }


def _spider_type(sql_type: str) -> str:
    sql_upper = sql_type.upper()
    if "INT" in sql_upper:
        return "number"
    if "REAL" in sql_upper or "FLOAT" in sql_upper or "DOUBLE" in sql_upper:
        return "number"
    return "text"


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _postgres_setup_sql(db: DbSpec, *, schema: str) -> str:
    lines = [
        f"DROP SCHEMA IF EXISTS {_quote_pg_ident(schema)} CASCADE;",
        f"CREATE SCHEMA {_quote_pg_ident(schema)};",
        f"SET search_path TO {_quote_pg_ident(schema)}, public;",
        "",
    ]
    for table in db.tables:
        declarations = [
            f"{_quote_pg_ident(column.name)} {_postgres_type(column.sql_type)}"
            for column in table.columns
        ]
        if table.primary_key:
            cols = ", ".join(_quote_pg_ident(column) for column in table.primary_key)
            declarations.append(f"PRIMARY KEY ({cols})")
        for fk in table.foreign_keys:
            declarations.append(
                "FOREIGN KEY "
                f"({_quote_pg_ident(fk.column)}) REFERENCES "
                f"{_quote_pg_ident(schema)}.{_quote_pg_ident(fk.ref_table)}"
                f"({_quote_pg_ident(fk.ref_column)})"
            )
        lines.append(
            f"CREATE TABLE {_quote_pg_ident(schema)}.{_quote_pg_ident(table.name)} ("
        )
        lines.extend(f"  {decl}," for decl in declarations[:-1])
        lines.append(f"  {declarations[-1]}")
        lines.append(");")
        if table.rows:
            columns = ", ".join(_quote_pg_ident(column.name) for column in table.columns)
            values = ",\n  ".join(
                "(" + ", ".join(_quote_pg_literal(value) for value in row) + ")"
                for row in table.rows
            )
            lines.append(
                f"INSERT INTO {_quote_pg_ident(schema)}.{_quote_pg_ident(table.name)} "
                f"({columns}) VALUES\n  {values};"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _postgres_type(sql_type: str) -> str:
    upper = sql_type.upper()
    if "INT" in upper:
        return "INTEGER"
    if "REAL" in upper or "FLOAT" in upper or "DOUBLE" in upper:
        return "DOUBLE PRECISION"
    return "TEXT"


def _quote_pg_ident(name: str) -> str:
    if not name:
        raise ValueError("Postgres identifier cannot be empty")
    if "\x00" in name or any(ch.isspace() for ch in name):
        raise ValueError(f"unsafe Postgres identifier {name!r}")
    return '"' + name.replace('"', '""') + '"'


def _quote_pg_literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int | float):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _mysql_setup_sql(db: DbSpec, *, database: str) -> str:
    lines = [
        f"DROP DATABASE IF EXISTS {_quote_mysql_ident(database)};",
        f"CREATE DATABASE {_quote_mysql_ident(database)} CHARACTER SET utf8mb4;",
        f"USE {_quote_mysql_ident(database)};",
        "",
    ]
    for table in db.tables:
        declarations = [
            f"{_quote_mysql_ident(column.name)} {_mysql_type(column.sql_type)}"
            for column in table.columns
        ]
        if table.primary_key:
            cols = ", ".join(_quote_mysql_ident(column) for column in table.primary_key)
            declarations.append(f"PRIMARY KEY ({cols})")
        for fk in table.foreign_keys:
            declarations.append(
                "FOREIGN KEY "
                f"({_quote_mysql_ident(fk.column)}) REFERENCES "
                f"{_quote_mysql_ident(fk.ref_table)}({_quote_mysql_ident(fk.ref_column)})"
            )
        lines.append(f"CREATE TABLE {_quote_mysql_ident(table.name)} (")
        lines.extend(f"  {decl}," for decl in declarations[:-1])
        lines.append(f"  {declarations[-1]}")
        lines.append(") ENGINE=InnoDB;")
        if table.rows:
            columns = ", ".join(_quote_mysql_ident(column.name) for column in table.columns)
            values = ",\n  ".join(
                "(" + ", ".join(_mysql_literal(value) for value in row) + ")"
                for row in table.rows
            )
            lines.append(
                f"INSERT INTO {_quote_mysql_ident(table.name)} "
                f"({columns}) VALUES\n  {values};"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _mysql_type(sql_type: str) -> str:
    upper = sql_type.upper()
    if "INT" in upper:
        return "INTEGER"
    if "REAL" in upper or "FLOAT" in upper or "DOUBLE" in upper:
        return "DOUBLE"
    return "VARCHAR(255)"


def _quote_mysql_ident(name: str) -> str:
    if not name:
        raise ValueError("MySQL identifier cannot be empty")
    if "\x00" in name or any(ch.isspace() for ch in name):
        raise ValueError(f"unsafe MySQL identifier {name!r}")
    return "`" + name.replace("`", "``") + "`"


def _mysql_literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int | float):
        return str(value)
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"
