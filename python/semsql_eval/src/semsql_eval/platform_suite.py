"""Platform-neutral complex NL-to-SQL comparison suite.

The suite is meant for SemSQL vs agentic NL-to-SQL tools such as Dataherald or
DB-GPT. It includes queries that should route, queries that should clarify, and
queries that should be rejected as unsafe or analytical rather than SQL lookup.
"""

from __future__ import annotations

import csv
import json
import random
import re
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Literal

from .fixtures import (
    ColumnSpec,
    DbSpec,
    ExampleSpec,
    ForeignKeySpec,
    MiniCorpus,
    TableSpec,
    build_corpus,
)

Disposition = Literal["route", "clarify", "reject", "known_gap"]
SchemaVariant = Literal["canonical", "semantic_alias", "random_alias"]

SUITE_VERSION = "platform-comparison-v1"


@dataclass(frozen=True)
class PlatformQueryCase:
    id: str
    question: str
    disposition: Disposition
    family: str
    difficulty: str
    expected_sql: str | None
    reason: str
    semsql_expectation: str
    competitor_signal: str


@dataclass(frozen=True)
class _SchemaAliasPlan:
    source_db_id: str
    target_db_id: str
    table_map: dict[str, str]
    column_map: dict[tuple[str, str], str]
    entity_terms: dict[str, tuple[str, ...]]
    field_terms: dict[tuple[str, str], tuple[str, ...]]


def build_platform_query_suite(
    out_dir: Path,
    *,
    schema_variant: SchemaVariant = "canonical",
    schema_alias_seed: int = 20260605,
) -> dict[str, object]:
    """Write a richer comparison suite to ``out_dir`` and return metadata."""
    db, cases, sidecars = _suite_schema_variant(
        out_dir=out_dir,
        db=GROWTH_OPS_DB,
        cases=PLATFORM_CASES,
        schema_variant=schema_variant,
        schema_alias_seed=schema_alias_seed,
    )
    corpus = MiniCorpus(dbs=(db,), examples=_route_examples(db.db_id, cases))
    build_corpus(out_dir, corpus)
    suite = {
        "schema_version": 1,
        "suite": SUITE_VERSION,
        "schema_variant": schema_variant,
        "schema_alias_seed": schema_alias_seed if schema_variant == "random_alias" else None,
        "db_id": db.db_id,
        "sqlite_path": str(
            out_dir
            / "database"
            / db.db_id
            / f"{db.db_id}.sqlite"
        ),
        "connection_uri": (
            "sqlite:///"
            + str(
                out_dir
                / "database"
                / db.db_id
                / f"{db.db_id}.sqlite"
            ).replace("\\", "/")
        ),
        "goal": (
            "Compare deterministic SemSQL QueryFrame routing with agentic "
            "NL-to-SQL systems on the same schema and questions."
        ),
        "platform_notes": {
            "semsql": (
                "Expected to be strongest on graph-grounded lookup, joins, "
                "structured literals, top-k aggregates, and fail-closed safety."
            ),
            "dataherald_style": (
                "Useful comparison for systems that consume table/column "
                "descriptions, sample rows, golden SQL examples, and confidence."
            ),
            "dbgpt_style": (
                "Useful comparison for agent workflows that can decompose, "
                "write SQL, inspect data, and summarize analytical results."
            ),
        },
        "schema_notes": _schema_notes(),
        "cases": [asdict(case) for case in cases],
        **sidecars,
    }
    (out_dir / "platform_query_suite.json").write_text(
        json.dumps(suite, indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / "questions.jsonl").write_text(
        "".join(json.dumps(asdict(case), sort_keys=True) + "\n" for case in cases),
        encoding="utf-8",
    )
    (out_dir / "expected.sql").write_text(_expected_sql_text(cases), encoding="utf-8")
    (out_dir / "README.md").write_text(render_platform_suite_markdown(suite), encoding="utf-8")
    return suite


def build_business_analytics_suite(
    out_dir: Path,
    *,
    schema_variant: SchemaVariant = "canonical",
    schema_alias_seed: int = 20260605,
) -> dict[str, object]:
    """Write a practical BI/CRM/growth/sales/ops suite to ``out_dir``."""
    db, cases, sidecars = _suite_schema_variant(
        out_dir=out_dir,
        db=BUSINESS_ANALYTICS_DB,
        cases=BUSINESS_ANALYTICS_CASES,
        schema_variant=schema_variant,
        schema_alias_seed=schema_alias_seed,
    )
    corpus = MiniCorpus(dbs=(db,), examples=_route_examples(db.db_id, cases))
    build_corpus(out_dir, corpus)
    suite = {
        "schema_version": 1,
        "suite": "business-analytics-v1",
        "schema_variant": schema_variant,
        "schema_alias_seed": schema_alias_seed if schema_variant == "random_alias" else None,
        "db_id": db.db_id,
        "sqlite_path": str(
            out_dir
            / "database"
            / db.db_id
            / f"{db.db_id}.sqlite"
        ),
        "connection_uri": (
            "sqlite:///"
            + str(
                out_dir
                / "database"
                / db.db_id
                / f"{db.db_id}.sqlite"
            ).replace("\\", "/")
        ),
        "goal": (
            "Stress practical business questions: BI dashboards, customer "
            "analytics, CRM pipeline, growth attribution, renewals, sales ops, "
            "support operations, and metric safety boundaries."
        ),
        "platform_notes": {
            "semsql": (
                "Should route only when the schema graph, literals, metric shape, "
                "and safety posture are locally validated."
            ),
            "dataherald_style": (
                "Good comparison for semantic-layer systems with table/column "
                "descriptions, sample values, curated golden SQL, and confidence."
            ),
            "dbgpt_style": (
                "Good comparison for agent workflows that inspect data, decompose "
                "analytical tasks, and generate SQL under database guardrails."
            ),
            "thoughtspot_defog_style": (
                "Good comparison for BI search systems with metric catalogs, "
                "governed dimensions, synonyms, and chart/table result shaping."
            ),
        },
        "schema_notes": _business_schema_notes(),
        "cases": [asdict(case) for case in cases],
        **sidecars,
    }
    (out_dir / "platform_query_suite.json").write_text(
        json.dumps(suite, indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / "questions.jsonl").write_text(
        "".join(
            json.dumps(asdict(case), sort_keys=True) + "\n"
            for case in cases
        ),
        encoding="utf-8",
    )
    (out_dir / "expected.sql").write_text(
        _expected_sql_text(cases), encoding="utf-8"
    )
    (out_dir / "README.md").write_text(render_platform_suite_markdown(suite), encoding="utf-8")
    return suite


def render_platform_suite_markdown(suite: dict[str, object]) -> str:
    cases = suite["cases"]
    assert isinstance(cases, list)
    title = "# Platform NL-to-SQL Comparison Suite"
    if suite.get("suite") == "business-analytics-v1":
        title = "# Business Analytics NL-to-SQL Suite"
    has_known_gap = any(
        isinstance(case, dict) and case.get("disposition") == "known_gap"
        for case in cases
    )
    lines = [
        title,
        "",
        f"- suite: `{suite['suite']}`",
        f"- schema variant: `{suite.get('schema_variant', 'canonical')}`",
        f"- db_id: `{suite['db_id']}`",
        f"- sqlite: `{suite['sqlite_path']}`",
        f"- connection URI: `{suite['connection_uri']}`",
        "",
        "## Purpose",
        "",
        str(suite["goal"]),
        "",
        "## How To Use",
        "",
        "1. Point the target NL-to-SQL system at the SQLite database.",
        "2. Ask each question from `questions.jsonl`.",
        "3. Compare SQL execution to `expected.sql` for `route` cases.",
        (
            "4. Count `clarify`, `reject`, and `known_gap` cases separately."
            if has_known_gap
            else "4. Count `clarify` and `reject` cases separately."
        ),
        "",
        "## Case Matrix",
        "",
        "| ID | Disposition | Family | Difficulty | Question |",
        "|---|---|---|---|---|",
    ]
    for case in cases:
        assert isinstance(case, dict)
        lines.append(
            "| `{id}` | `{disposition}` | `{family}` | `{difficulty}` | {question} |".format(
                id=case["id"],
                disposition=case["disposition"],
                family=case["family"],
                difficulty=case["difficulty"],
                question=str(case["question"]).replace("|", "\\|"),
        )
    )
    if suite.get("schema_variant") == "random_alias":
        lines.insert(4, f"- schema alias seed: `{suite['schema_alias_seed']}`")
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "- `route`: expected to produce executable SQL over the provided schema.",
            "- `clarify`: expected to ask a smaller disambiguation question.",
            "- `reject`: expected to refuse unsafe/non-SQL action.",
        ]
    )
    if has_known_gap:
        lines.append(
            "- `known_gap`: useful stress case, but not a current SemSQL acceptance target."
        )
    lines.append("")
    return "\n".join(lines)


def _route_examples(
    db_id: str, cases: tuple[PlatformQueryCase, ...]
) -> tuple[ExampleSpec, ...]:
    return tuple(
        ExampleSpec(db_id, case.question, case.expected_sql)
        for case in cases
        if case.disposition == "route" and case.expected_sql is not None
    )


def _expected_sql_text(cases: tuple[PlatformQueryCase, ...] | None = None) -> str:
    if cases is None:
        cases = PLATFORM_CASES
    chunks = []
    for case in cases:
        if case.expected_sql is None:
            continue
        chunks.append(f"-- {case.id}: {case.question}\n{case.expected_sql};\n")
    return "\n".join(chunks)


def _suite_schema_variant(
    *,
    out_dir: Path,
    db: DbSpec,
    cases: tuple[PlatformQueryCase, ...],
    schema_variant: SchemaVariant,
    schema_alias_seed: int,
) -> tuple[DbSpec, tuple[PlatformQueryCase, ...], dict[str, object]]:
    if schema_variant == "canonical":
        return db, cases, {}
    if schema_variant not in {"semantic_alias", "random_alias"}:
        raise ValueError(f"unknown schema variant: {schema_variant}")
    plan = _schema_alias_plan_for(db.db_id)
    if schema_variant == "random_alias":
        plan = _random_schema_alias_plan(db, plan, seed=schema_alias_seed)
    renamed_db = _rename_db_spec(db, plan)
    renamed_cases = tuple(_rename_case_sql(case, plan) for case in cases)
    raw_sidecars = _write_schema_alias_sidecars(out_dir, source_db=db, plan=plan)
    sidecars: dict[str, object] = {}
    for key, value in raw_sidecars.items():
        sidecars[key] = value
    sidecars["source_db_id"] = db.db_id
    if schema_variant == "random_alias":
        sidecars["schema_alias_seed"] = schema_alias_seed
        sidecars["schema_alias_plan"] = {
            "table_map": dict(plan.table_map),
            "column_map": {
                f"{table}.{column}": target
                for (table, column), target in sorted(plan.column_map.items())
            },
        }
    return renamed_db, renamed_cases, sidecars


def _schema_alias_plan_for(db_id: str) -> _SchemaAliasPlan:
    if db_id == "growth_ops":
        return _SchemaAliasPlan(
            source_db_id=db_id,
            target_db_id="growth_ops_semantic_alias",
            table_map={
                "regions": "territories",
                "plans": "packages",
                "agents": "staff_members",
                "accounts": "clients",
                "invoices": "billing_records",
                "tickets": "support_cases",
                "events": "customer_events",
            },
            column_map={
                ("regions", "name"): "region_name",
                ("plans", "name"): "package_name",
                ("plans", "tier"): "package_tier",
                ("plans", "monthly_price"): "monthly_fee",
                ("agents", "full_name"): "staff_name",
                ("agents", "team"): "team_name",
                ("agents", "active"): "is_active",
                ("accounts", "company_name"): "client_name",
                ("accounts", "status"): "client_status",
                ("accounts", "region_id"): "territory_id",
                ("accounts", "plan_id"): "package_id",
                ("accounts", "owner_agent_id"): "success_owner_id",
                ("accounts", "signup_date"): "joined_on",
                ("accounts", "external_code"): "client_code",
                ("invoices", "account_id"): "client_id",
                ("invoices", "amount"): "billing_amount",
                ("invoices", "status"): "billing_status",
                ("invoices", "issued_on"): "invoice_date",
                ("invoices", "paid_on"): "settled_on",
                ("tickets", "account_id"): "client_id",
                ("tickets", "assignee_id"): "assigned_staff_id",
                ("tickets", "priority"): "case_priority",
                ("tickets", "status"): "case_status",
                ("tickets", "opened_on"): "opened_on",
                ("tickets", "resolved_on"): "resolved_on",
                ("tickets", "resolution_hours"): "resolution_hours",
                ("events", "account_id"): "client_id",
                ("events", "event_type"): "event_name",
                ("events", "occurred_on"): "event_date",
            },
            entity_terms={
                "regions": ("regions", "region", "territories", "territory"),
                "plans": ("plans", "plan", "packages", "package", "tier"),
                "agents": (
                    "agents",
                    "agent",
                    "support agent",
                    "owner",
                    "staff",
                    "staff member",
                ),
                "accounts": (
                    "accounts",
                    "account",
                    "customers",
                    "customer",
                    "clients",
                    "client",
                ),
                "invoices": ("invoices", "invoice", "billing", "bills"),
                "tickets": ("tickets", "ticket", "support tickets", "support cases"),
                "events": ("events", "event", "login events", "customer events"),
            },
            field_terms={},
        )
    if db_id == "business_analytics":
        return _SchemaAliasPlan(
            source_db_id=db_id,
            target_db_id="business_analytics_semantic_alias",
            table_map={
                "regions": "territories",
                "reps": "team_members",
                "accounts": "organizations",
                "campaigns": "marketing_programs",
                "leads": "prospects",
                "opportunities": "deals",
                "subscriptions": "recurring_contracts",
                "invoices": "billing_records",
                "activities": "touchpoints",
                "tickets": "support_cases",
                "usage_events": "product_events",
                "nps_responses": "survey_responses",
                "contacts": "people",
            },
            column_map={
                ("regions", "name"): "region_name",
                ("reps", "full_name"): "person_name",
                ("reps", "team"): "team_name",
                ("reps", "active"): "is_enabled",
                ("accounts", "company_name"): "organization_name",
                ("accounts", "domain"): "website_domain",
                ("accounts", "segment"): "market_segment",
                ("accounts", "industry"): "industry_name",
                ("accounts", "region_id"): "territory_id",
                ("accounts", "owner_rep_id"): "owner_member_id",
                ("accounts", "status"): "account_state",
                ("accounts", "lifecycle_stage"): "lifecycle_state",
                ("accounts", "created_on"): "created_date",
                ("accounts", "renewal_date"): "renewal_due_on",
                ("accounts", "arr"): "annual_recurring_revenue",
                ("accounts", "health_score"): "health_rating",
                ("campaigns", "name"): "program_name",
                ("campaigns", "channel"): "marketing_channel",
                ("campaigns", "started_on"): "launch_date",
                ("leads", "company_name"): "lead_company",
                ("leads", "source_campaign_id"): "program_id",
                ("leads", "source_channel"): "acquisition_channel",
                ("leads", "created_on"): "captured_on",
                ("leads", "converted_account_id"): "converted_organization_id",
                ("opportunities", "account_id"): "organization_id",
                ("opportunities", "owner_rep_id"): "owner_member_id",
                ("opportunities", "stage"): "deal_stage",
                ("opportunities", "amount"): "deal_amount",
                ("opportunities", "created_on"): "created_date",
                ("opportunities", "close_date"): "expected_close_date",
                ("opportunities", "source"): "deal_source",
                ("opportunities", "lost_reason"): "loss_reason",
                ("subscriptions", "account_id"): "organization_id",
                ("subscriptions", "plan"): "package_name",
                ("subscriptions", "mrr"): "monthly_recurring_revenue",
                ("subscriptions", "status"): "subscription_state",
                ("subscriptions", "started_on"): "started_date",
                ("subscriptions", "ended_on"): "ended_date",
                ("invoices", "account_id"): "organization_id",
                ("invoices", "amount"): "invoice_amount",
                ("invoices", "status"): "invoice_state",
                ("invoices", "issued_on"): "issued_date",
                ("invoices", "paid_on"): "paid_date",
                ("activities", "account_id"): "organization_id",
                ("activities", "rep_id"): "member_id",
                ("activities", "activity_type"): "touchpoint_type",
                ("activities", "occurred_on"): "activity_date",
                ("tickets", "account_id"): "organization_id",
                ("tickets", "assignee_rep_id"): "assigned_member_id",
                ("tickets", "severity"): "severity_level",
                ("tickets", "status"): "case_state",
                ("tickets", "opened_on"): "opened_date",
                ("tickets", "resolved_on"): "resolved_date",
                ("tickets", "sla_breached"): "sla_missed",
                ("tickets", "resolution_hours"): "time_to_resolve_hours",
                ("usage_events", "account_id"): "organization_id",
                ("usage_events", "event_type"): "event_name",
                ("usage_events", "occurred_on"): "event_date",
                ("nps_responses", "account_id"): "organization_id",
                ("nps_responses", "responded_on"): "response_date",
                ("contacts", "account_id"): "organization_id",
                ("contacts", "created_on"): "created_date",
            },
            entity_terms={
                "regions": ("regions", "region", "territories", "territory"),
                "reps": (
                    "reps",
                    "rep",
                    "owner",
                    "account owner",
                    "support rep",
                    "sales rep",
                    "team members",
                ),
                "accounts": (
                    "accounts",
                    "account",
                    "customers",
                    "customer",
                    "clients",
                    "organizations",
                ),
                "campaigns": ("campaigns", "campaign", "marketing programs"),
                "leads": ("leads", "lead", "signups", "prospects"),
                "opportunities": ("opportunities", "opportunity", "pipeline", "deals"),
                "subscriptions": ("subscriptions", "subscription", "mrr", "plans"),
                "invoices": ("invoices", "invoice", "billing"),
                "activities": ("activities", "activity", "sales activity", "touchpoints"),
                "tickets": ("tickets", "ticket", "support tickets", "support cases"),
                "usage_events": ("usage events", "product events", "login", "activation"),
                "nps_responses": ("nps", "nps responses", "survey responses"),
                "contacts": ("contacts", "contact emails", "people"),
            },
            field_terms={
                ("accounts", "arr"): ("arr", "annual recurring revenue"),
                ("subscriptions", "mrr"): ("mrr", "monthly recurring revenue"),
                ("tickets", "sla_breached"): ("sla breached", "sla breach", "sla missed"),
            },
        )
    raise ValueError(f"no schema-alias plan for db_id {db_id!r}")


_RANDOM_ALIAS_TABLE_STEMS = (
    "amber",
    "banyan",
    "cedar",
    "dahlia",
    "ember",
    "fennel",
    "garnet",
    "harbor",
    "indigo",
    "juniper",
    "krypton",
    "lumen",
    "marble",
    "nebula",
    "onyx",
    "prairie",
    "quartz",
    "raven",
    "saffron",
    "topaz",
    "umber",
    "velvet",
    "willow",
    "xenon",
    "yarrow",
    "zephyr",
)

_RANDOM_ALIAS_COLUMN_STEMS = (
    "arc",
    "beam",
    "crest",
    "drift",
    "echo",
    "flux",
    "glow",
    "hatch",
    "ion",
    "jolt",
    "keel",
    "loom",
    "mote",
    "nimbus",
    "opal",
    "pulse",
    "quill",
    "rill",
    "shard",
    "tide",
    "uplink",
    "vapor",
    "wisp",
    "xylem",
    "yonder",
    "zinc",
)


def _random_schema_alias_plan(
    db: DbSpec, base_plan: _SchemaAliasPlan, *, seed: int
) -> _SchemaAliasPlan:
    rng = random.Random(f"{db.db_id}:{seed}:schema-alias")
    table_stems = list(_RANDOM_ALIAS_TABLE_STEMS)
    column_stems = list(_RANDOM_ALIAS_COLUMN_STEMS)
    rng.shuffle(table_stems)
    rng.shuffle(column_stems)

    table_map: dict[str, str] = {}
    column_map: dict[tuple[str, str], str] = {}
    for table_index, table in enumerate(db.tables, start=1):
        stem = table_stems[(table_index - 1) % len(table_stems)]
        table_map[table.name] = f"t_{stem}_{table_index:02d}"

        used_columns = {"id"}
        for column_index, column in enumerate(table.columns, start=1):
            if column.name == "id":
                continue
            stem_index = (table_index * 7 + column_index * 11) % len(column_stems)
            stem = column_stems[stem_index]
            candidate = f"c_{stem}_{column_index:02d}"
            while candidate in used_columns:
                stem_index = (stem_index + 1) % len(column_stems)
                candidate = f"c_{column_stems[stem_index]}_{column_index:02d}"
            used_columns.add(candidate)
            column_map[(table.name, column.name)] = candidate

    return _SchemaAliasPlan(
        source_db_id=base_plan.source_db_id,
        target_db_id=f"{db.db_id}_random_alias_{seed}",
        table_map=table_map,
        column_map=column_map,
        entity_terms=base_plan.entity_terms,
        field_terms=base_plan.field_terms,
    )


def _rename_db_spec(db: DbSpec, plan: _SchemaAliasPlan) -> DbSpec:
    return DbSpec(
        db_id=plan.target_db_id,
        tables=tuple(_rename_table_spec(table, plan) for table in db.tables),
    )


def _rename_table_spec(table: TableSpec, plan: _SchemaAliasPlan) -> TableSpec:
    column_names = {
        column.name: plan.column_map.get((table.name, column.name), column.name)
        for column in table.columns
    }
    return TableSpec(
        name=plan.table_map.get(table.name, table.name),
        columns=tuple(
            ColumnSpec(column_names[column.name], column.sql_type)
            for column in table.columns
        ),
        rows=table.rows,
        primary_key=tuple(column_names[column] for column in table.primary_key),
        foreign_keys=tuple(
            ForeignKeySpec(
                column_names[fk.column],
                plan.table_map.get(fk.ref_table, fk.ref_table),
                plan.column_map.get((fk.ref_table, fk.ref_column), fk.ref_column),
            )
            for fk in table.foreign_keys
        ),
    )


def _rename_case_sql(
    case: PlatformQueryCase, plan: _SchemaAliasPlan
) -> PlatformQueryCase:
    if case.expected_sql is None:
        return case
    return replace(case, expected_sql=_rename_sql(case.expected_sql, plan))


def _rename_sql(sql: str, plan: _SchemaAliasPlan) -> str:
    renamed = sql
    dotted_replacements: list[tuple[str, str]] = []
    for (table, column), new_column in plan.column_map.items():
        new_table = plan.table_map.get(table, table)
        dotted_replacements.append((f"{table}.{column}", f"{new_table}.{new_column}"))
    for old, new in sorted(dotted_replacements, key=lambda item: len(item[0]), reverse=True):
        renamed = re.sub(rf"\b{re.escape(old)}\b", new, renamed)
    for old, new in sorted(plan.table_map.items(), key=lambda item: len(item[0]), reverse=True):
        renamed = re.sub(rf"\b{re.escape(old)}\b", new, renamed)
    return renamed


def _write_schema_alias_sidecars(
    out_dir: Path, *, source_db: DbSpec, plan: _SchemaAliasPlan
) -> dict[str, str]:
    vocab_path = out_dir / "semantic_alias_vocab.jsonl"
    schema_description_dir = out_dir / "database_description"
    schema_description_dir.mkdir(parents=True, exist_ok=True)
    _write_schema_alias_vocab(vocab_path, source_db=source_db, plan=plan)
    _write_schema_description_csvs(
        schema_description_dir, source_db=source_db, plan=plan
    )
    return {
        "vocab_jsonl": str(vocab_path),
        "schema_description_dir": str(schema_description_dir),
    }


def _write_schema_alias_vocab(
    path: Path, *, source_db: DbSpec, plan: _SchemaAliasPlan
) -> None:
    rows: list[dict[str, object]] = []
    line = 1
    for table in source_db.tables:
        target_table = plan.table_map.get(table.name, table.name)
        for term in _entity_terms(table.name, target_table, plan):
            rows.append(
                _vocab_row(
                    term=term,
                    canonical={"kind": "entity", "entity": target_table},
                    line=line,
                )
            )
            line += 1
        for column in table.columns:
            target_field = (
                f"{target_table}.{plan.column_map.get((table.name, column.name), column.name)}"
            )
            for term in _field_terms(table.name, column.name, target_field, plan):
                rows.append(
                    _vocab_row(
                        term=term,
                        canonical={"kind": "field", "field": target_field},
                        line=line,
                    )
                )
                line += 1
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _vocab_row(
    *, term: str, canonical: dict[str, str], line: int
) -> dict[str, object]:
    return {
        "term": term,
        "canonical": canonical,
        "confidence": 0.95,
        "locator": {
            "file": "semantic_alias_suite",
            "line": line,
            "layer": 6,
            "extractor": "semsql-eval:schema-variant",
        },
    }


def _write_schema_description_csvs(
    out_dir: Path, *, source_db: DbSpec, plan: _SchemaAliasPlan
) -> None:
    for table in source_db.tables:
        target_table = plan.table_map.get(table.name, table.name)
        path = out_dir / f"{target_table}.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "table",
                    "original_column_name",
                    "display_label",
                    "column_description",
                    "value_description",
                ],
            )
            writer.writeheader()
            for column in table.columns:
                target_column = plan.column_map.get((table.name, column.name), column.name)
                label = _display_label_for(table.name, column.name, target_column, plan)
                writer.writerow(
                    {
                        "table": target_table,
                        "original_column_name": target_column,
                        "display_label": label,
                        "column_description": label,
                        "value_description": _value_description(table, column.name),
                    }
                )


def _entity_terms(
    source_table: str, target_table: str, plan: _SchemaAliasPlan
) -> tuple[str, ...]:
    raw_terms = (
        *plan.entity_terms.get(source_table, ()),
        source_table,
        _singular(source_table),
        target_table,
        _singular(target_table),
    )
    return _dedupe_terms(raw_terms)


def _field_terms(
    source_table: str,
    source_column: str,
    target_field: str,
    plan: _SchemaAliasPlan,
) -> tuple[str, ...]:
    target_column = target_field.split(".", 1)[1]
    raw_terms = (
        *plan.field_terms.get((source_table, source_column), ()),
        source_column,
        source_column.replace("_", " "),
        target_column,
        target_column.replace("_", " "),
    )
    return _dedupe_terms(raw_terms)


def _display_label_for(
    source_table: str,
    source_column: str,
    target_column: str,
    plan: _SchemaAliasPlan,
) -> str:
    terms = plan.field_terms.get((source_table, source_column), ())
    if terms:
        return _title_label(terms[0])
    return _title_label(source_column)


def _value_description(table: TableSpec, column_name: str) -> str:
    column_index = next(
        (idx for idx, column in enumerate(table.columns) if column.name == column_name),
        None,
    )
    if column_index is None:
        return ""
    values = {
        str(row[column_index])
        for row in table.rows
        if row[column_index] is not None and not _looks_sensitive_value(str(row[column_index]))
    }
    if not (1 <= len(values) <= 12):
        return ""
    return "; ".join(f"{value} = {value}" for value in sorted(values))


def _looks_sensitive_value(value: str) -> bool:
    return "@" in value


def _dedupe_terms(terms: tuple[str, ...]) -> tuple[str, ...]:
    out: list[str] = []
    seen: set[str] = set()
    for term in terms:
        normalized = " ".join(term.replace("_", " ").split()).strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return tuple(out)


def _singular(value: str) -> str:
    if value.endswith("ies"):
        return f"{value[:-3]}y"
    if value.endswith("s"):
        return value[:-1]
    return value


def _title_label(value: str) -> str:
    return " ".join(part.capitalize() for part in value.replace("_", " ").split())


def _schema_notes() -> dict[str, str]:
    return {
        "accounts": "Customers/accounts with plan, region, status, signup date, and owner.",
        "plans": "Subscription tier and monthly price.",
        "regions": "Sales geography.",
        "agents": "Account/support owners.",
        "invoices": "Billing facts with amount, status, issue date, and paid date.",
        "tickets": "Support facts with priority, status, open date, and resolution hours.",
        "events": "Product/customer events such as login, cancellation, and upgrade.",
    }


def _business_schema_notes() -> dict[str, str]:
    return {
        "accounts": "Customer accounts with segment, industry, region, owner, lifecycle, ARR, renewal date, and health score.",
        "reps": "Sales, success, and support owners.",
        "regions": "Sales and customer geography.",
        "opportunities": "CRM pipeline facts with stage, owner, amount, source, and close dates.",
        "campaigns": "Marketing campaigns and channels.",
        "leads": "Top-of-funnel leads with source, status, and conversion account.",
        "subscriptions": "Recurring revenue facts with plan, MRR, status, start, and end dates.",
        "invoices": "Billing facts with amount, status, issue date, and payment date.",
        "activities": "CRM/customer touchpoints such as calls and emails.",
        "tickets": "Support facts with severity, status, SLA breach flag, and resolution time.",
        "usage_events": "Product usage facts such as login and activation events.",
        "nps_responses": "Customer survey facts for NPS-style BI questions.",
    }


GROWTH_OPS_DB = DbSpec(
    db_id="growth_ops",
    tables=(
        TableSpec(
            name="regions",
            columns=(ColumnSpec("id", "INTEGER"), ColumnSpec("name", "TEXT")),
            rows=((1, "EMEA"), (2, "APAC"), (3, "LATAM")),
            primary_key=("id",),
        ),
        TableSpec(
            name="plans",
            columns=(
                ColumnSpec("id", "INTEGER"),
                ColumnSpec("name", "TEXT"),
                ColumnSpec("tier", "TEXT"),
                ColumnSpec("monthly_price", "REAL"),
            ),
            rows=(
                (1, "Starter", "self_serve", 49.0),
                (2, "Growth", "business", 199.0),
                (3, "Enterprise", "enterprise", 999.0),
            ),
            primary_key=("id",),
        ),
        TableSpec(
            name="agents",
            columns=(
                ColumnSpec("id", "INTEGER"),
                ColumnSpec("full_name", "TEXT"),
                ColumnSpec("team", "TEXT"),
                ColumnSpec("active", "INTEGER"),
            ),
            rows=(
                (1, "Mina Patel", "success", 1),
                (2, "Jon Bell", "support", 1),
                (3, "Ada Cruz", "support", 0),
            ),
            primary_key=("id",),
        ),
        TableSpec(
            name="accounts",
            columns=(
                ColumnSpec("id", "INTEGER"),
                ColumnSpec("company_name", "TEXT"),
                ColumnSpec("status", "TEXT"),
                ColumnSpec("region_id", "INTEGER"),
                ColumnSpec("plan_id", "INTEGER"),
                ColumnSpec("owner_agent_id", "INTEGER"),
                ColumnSpec("signup_date", "TEXT"),
                ColumnSpec("external_code", "TEXT"),
            ),
            rows=(
                (10, "Acme Cloud", "active", 1, 3, 1, "2024-01-05", "ACME-001"),
                (11, "Orbit Labs", "active", 2, 2, 2, "2024-02-14", "ORB-77"),
                (12, "Northstar Health", "paused", 1, 2, 1, "2023-12-20", "NHS-004"),
                (13, "Vector Retail", "cancelled", 3, 1, 3, "2023-10-11", "VEC-9"),
            ),
            primary_key=("id",),
            foreign_keys=(
                ForeignKeySpec("region_id", "regions", "id"),
                ForeignKeySpec("plan_id", "plans", "id"),
                ForeignKeySpec("owner_agent_id", "agents", "id"),
            ),
        ),
        TableSpec(
            name="invoices",
            columns=(
                ColumnSpec("id", "INTEGER"),
                ColumnSpec("account_id", "INTEGER"),
                ColumnSpec("amount", "REAL"),
                ColumnSpec("status", "TEXT"),
                ColumnSpec("issued_on", "TEXT"),
                ColumnSpec("paid_on", "TEXT"),
            ),
            rows=(
                (100, 10, 1200.0, "paid", "2024-02-01", "2024-02-10"),
                (101, 10, 1350.0, "open", "2024-03-01", None),
                (102, 11, 450.0, "paid", "2024-02-05", "2024-02-18"),
                (103, 12, 600.0, "overdue", "2024-02-07", None),
                (104, 13, 99.0, "void", "2024-01-15", None),
            ),
            primary_key=("id",),
            foreign_keys=(ForeignKeySpec("account_id", "accounts", "id"),),
        ),
        TableSpec(
            name="tickets",
            columns=(
                ColumnSpec("id", "INTEGER"),
                ColumnSpec("account_id", "INTEGER"),
                ColumnSpec("assignee_id", "INTEGER"),
                ColumnSpec("priority", "TEXT"),
                ColumnSpec("status", "TEXT"),
                ColumnSpec("opened_on", "TEXT"),
                ColumnSpec("resolved_on", "TEXT"),
                ColumnSpec("resolution_hours", "REAL"),
            ),
            rows=(
                (200, 10, 2, "high", "resolved", "2024-02-03", "2024-02-04", 9.5),
                (201, 10, 2, "low", "open", "2024-03-02", None, None),
                (202, 11, 2, "high", "resolved", "2024-02-20", "2024-02-22", 31.0),
                (203, 12, 3, "medium", "resolved", "2024-02-12", "2024-02-13", 11.0),
                (204, 13, 3, "high", "open", "2024-01-20", None, None),
            ),
            primary_key=("id",),
            foreign_keys=(
                ForeignKeySpec("account_id", "accounts", "id"),
                ForeignKeySpec("assignee_id", "agents", "id"),
            ),
        ),
        TableSpec(
            name="events",
            columns=(
                ColumnSpec("id", "INTEGER"),
                ColumnSpec("account_id", "INTEGER"),
                ColumnSpec("event_type", "TEXT"),
                ColumnSpec("occurred_on", "TEXT"),
            ),
            rows=(
                (300, 10, "login", "2024-02-11"),
                (301, 10, "upgrade", "2024-02-12"),
                (302, 11, "login", "2024-03-01"),
                (303, 13, "cancelled", "2024-01-31"),
            ),
            primary_key=("id",),
            foreign_keys=(ForeignKeySpec("account_id", "accounts", "id"),),
        ),
    ),
)


BUSINESS_ANALYTICS_DB = DbSpec(
    db_id="business_analytics",
    tables=(
        TableSpec(
            name="regions",
            columns=(ColumnSpec("id", "INTEGER"), ColumnSpec("name", "TEXT")),
            rows=((1, "EMEA"), (2, "APAC"), (3, "NA"), (4, "LATAM")),
            primary_key=("id",),
        ),
        TableSpec(
            name="reps",
            columns=(
                ColumnSpec("id", "INTEGER"),
                ColumnSpec("full_name", "TEXT"),
                ColumnSpec("team", "TEXT"),
                ColumnSpec("active", "INTEGER"),
            ),
            rows=(
                (1, "Mia Chen", "sales", 1),
                (2, "Noah Smith", "sales", 1),
                (3, "Lina Okafor", "success", 1),
                (4, "Omar Diaz", "support", 1),
                (5, "Ada Cruz", "success", 0),
            ),
            primary_key=("id",),
        ),
        TableSpec(
            name="accounts",
            columns=(
                ColumnSpec("id", "INTEGER"),
                ColumnSpec("company_name", "TEXT"),
                ColumnSpec("domain", "TEXT"),
                ColumnSpec("segment", "TEXT"),
                ColumnSpec("industry", "TEXT"),
                ColumnSpec("region_id", "INTEGER"),
                ColumnSpec("owner_rep_id", "INTEGER"),
                ColumnSpec("status", "TEXT"),
                ColumnSpec("lifecycle_stage", "TEXT"),
                ColumnSpec("created_on", "TEXT"),
                ColumnSpec("renewal_date", "TEXT"),
                ColumnSpec("arr", "REAL"),
                ColumnSpec("health_score", "REAL"),
            ),
            rows=(
                (
                    100,
                    "Acme Cloud",
                    "acme.com",
                    "enterprise",
                    "SaaS",
                    1,
                    3,
                    "active",
                    "customer",
                    "2023-12-10",
                    "2024-03-20",
                    120000.0,
                    82.0,
                ),
                (
                    101,
                    "Orbit Labs",
                    "orbit.io",
                    "mid_market",
                    "SaaS",
                    2,
                    3,
                    "active",
                    "customer",
                    "2024-02-14",
                    "2024-06-10",
                    54000.0,
                    67.0,
                ),
                (
                    102,
                    "Northstar Health",
                    "northstar.health",
                    "enterprise",
                    "Healthcare",
                    3,
                    5,
                    "active",
                    "customer",
                    "2023-09-30",
                    "2024-03-25",
                    180000.0,
                    38.0,
                ),
                (
                    103,
                    "Vector Retail",
                    "vector.retail",
                    "smb",
                    "Retail",
                    4,
                    2,
                    "churned",
                    "churned",
                    "2023-05-01",
                    "2024-01-31",
                    24000.0,
                    22.0,
                ),
                (
                    104,
                    "BluePeak Finance",
                    "bluepeak.finance",
                    "mid_market",
                    "Finance",
                    1,
                    1,
                    "active",
                    "customer",
                    "2024-01-18",
                    "2024-04-15",
                    72000.0,
                    75.0,
                ),
                (
                    105,
                    "GreenField Energy",
                    "greenfield.energy",
                    "enterprise",
                    "Energy",
                    1,
                    1,
                    "active",
                    "customer",
                    "2024-01-11",
                    "2024-03-05",
                    150000.0,
                    55.0,
                ),
            ),
            primary_key=("id",),
            foreign_keys=(
                ForeignKeySpec("region_id", "regions", "id"),
                ForeignKeySpec("owner_rep_id", "reps", "id"),
            ),
        ),
        TableSpec(
            name="campaigns",
            columns=(
                ColumnSpec("id", "INTEGER"),
                ColumnSpec("name", "TEXT"),
                ColumnSpec("channel", "TEXT"),
                ColumnSpec("started_on", "TEXT"),
            ),
            rows=(
                (10, "February Search", "paid_search", "2024-02-01"),
                (11, "Spring Webinar", "webinar", "2024-02-10"),
                (12, "Partner Push", "partner", "2024-03-01"),
                (13, "Field Event", "event", "2024-01-20"),
            ),
            primary_key=("id",),
        ),
        TableSpec(
            name="leads",
            columns=(
                ColumnSpec("id", "INTEGER"),
                ColumnSpec("company_name", "TEXT"),
                ColumnSpec("source_campaign_id", "INTEGER"),
                ColumnSpec("source_channel", "TEXT"),
                ColumnSpec("status", "TEXT"),
                ColumnSpec("created_on", "TEXT"),
                ColumnSpec("converted_account_id", "INTEGER"),
            ),
            rows=(
                (200, "Acme Cloud", 13, "event", "converted", "2024-01-05", 100),
                (201, "Orbit Labs", 10, "paid_search", "converted", "2024-02-14", 101),
                (202, "Northstar Health", 11, "webinar", "converted", "2024-02-20", 102),
                (203, "BluePeak Finance", 12, "partner", "converted", "2024-03-02", 104),
                (204, "Atlas AI", 10, "paid_search", "new", "2024-02-21", None),
                (205, "RiverBank", 10, "paid_search", "qualified", "2024-02-25", None),
                (206, "GreenField Energy", 13, "event", "converted", "2024-01-22", 105),
                (207, "Zen Retail", 11, "webinar", "new", "2024-02-28", None),
            ),
            primary_key=("id",),
            foreign_keys=(
                ForeignKeySpec("source_campaign_id", "campaigns", "id"),
                ForeignKeySpec("converted_account_id", "accounts", "id"),
            ),
        ),
        TableSpec(
            name="opportunities",
            columns=(
                ColumnSpec("id", "INTEGER"),
                ColumnSpec("account_id", "INTEGER"),
                ColumnSpec("owner_rep_id", "INTEGER"),
                ColumnSpec("stage", "TEXT"),
                ColumnSpec("amount", "REAL"),
                ColumnSpec("created_on", "TEXT"),
                ColumnSpec("close_date", "TEXT"),
                ColumnSpec("source", "TEXT"),
                ColumnSpec("lost_reason", "TEXT"),
            ),
            rows=(
                (300, 100, 1, "closed_won", 120000.0, "2023-11-20", "2024-01-15", "event", None),
                (301, 101, 2, "negotiation", 60000.0, "2024-02-18", "2024-04-20", "paid_search", None),
                (302, 102, 1, "proposal", 200000.0, "2024-01-25", "2024-05-12", "webinar", None),
                (303, 104, 2, "qualified", 80000.0, "2024-03-02", "2024-06-25", "partner", None),
                (304, 103, 1, "closed_lost", 30000.0, "2024-01-10", "2024-02-10", "paid_search", "price"),
                (305, 105, 1, "closed_won", 150000.0, "2024-01-15", "2024-03-01", "event", None),
                (306, 105, 2, "proposal", 90000.0, "2024-03-01", "2024-04-15", "expansion", None),
            ),
            primary_key=("id",),
            foreign_keys=(
                ForeignKeySpec("account_id", "accounts", "id"),
                ForeignKeySpec("owner_rep_id", "reps", "id"),
            ),
        ),
        TableSpec(
            name="subscriptions",
            columns=(
                ColumnSpec("id", "INTEGER"),
                ColumnSpec("account_id", "INTEGER"),
                ColumnSpec("plan", "TEXT"),
                ColumnSpec("mrr", "REAL"),
                ColumnSpec("status", "TEXT"),
                ColumnSpec("started_on", "TEXT"),
                ColumnSpec("ended_on", "TEXT"),
            ),
            rows=(
                (400, 100, "enterprise", 10000.0, "active", "2024-01-15", None),
                (401, 101, "growth", 4500.0, "active", "2024-02-20", None),
                (402, 102, "enterprise", 15000.0, "active", "2023-10-01", None),
                (403, 103, "starter", 2000.0, "cancelled", "2023-05-01", "2024-01-31"),
                (404, 104, "growth", 6000.0, "active", "2024-03-05", None),
                (405, 105, "enterprise", 12500.0, "active", "2024-03-01", None),
            ),
            primary_key=("id",),
            foreign_keys=(ForeignKeySpec("account_id", "accounts", "id"),),
        ),
        TableSpec(
            name="invoices",
            columns=(
                ColumnSpec("id", "INTEGER"),
                ColumnSpec("account_id", "INTEGER"),
                ColumnSpec("amount", "REAL"),
                ColumnSpec("status", "TEXT"),
                ColumnSpec("issued_on", "TEXT"),
                ColumnSpec("paid_on", "TEXT"),
            ),
            rows=(
                (500, 100, 10000.0, "paid", "2024-02-01", "2024-02-10"),
                (501, 101, 4500.0, "paid", "2024-02-05", "2024-02-18"),
                (502, 102, 15000.0, "overdue", "2024-02-07", None),
                (503, 103, 2000.0, "void", "2024-01-15", None),
                (504, 104, 6000.0, "open", "2024-03-01", None),
                (505, 105, 12500.0, "overdue", "2024-03-01", None),
            ),
            primary_key=("id",),
            foreign_keys=(ForeignKeySpec("account_id", "accounts", "id"),),
        ),
        TableSpec(
            name="activities",
            columns=(
                ColumnSpec("id", "INTEGER"),
                ColumnSpec("account_id", "INTEGER"),
                ColumnSpec("rep_id", "INTEGER"),
                ColumnSpec("activity_type", "TEXT"),
                ColumnSpec("occurred_on", "TEXT"),
            ),
            rows=(
                (600, 100, 3, "business_review", "2024-03-05"),
                (601, 101, 3, "email", "2024-02-28"),
                (602, 102, 5, "call", "2024-01-15"),
                (603, 104, 1, "demo", "2024-03-04"),
                (604, 105, 1, "email", "2024-03-02"),
            ),
            primary_key=("id",),
            foreign_keys=(
                ForeignKeySpec("account_id", "accounts", "id"),
                ForeignKeySpec("rep_id", "reps", "id"),
            ),
        ),
        TableSpec(
            name="tickets",
            columns=(
                ColumnSpec("id", "INTEGER"),
                ColumnSpec("account_id", "INTEGER"),
                ColumnSpec("assignee_rep_id", "INTEGER"),
                ColumnSpec("severity", "TEXT"),
                ColumnSpec("status", "TEXT"),
                ColumnSpec("opened_on", "TEXT"),
                ColumnSpec("resolved_on", "TEXT"),
                ColumnSpec("sla_breached", "INTEGER"),
                ColumnSpec("resolution_hours", "REAL"),
            ),
            rows=(
                (700, 100, 4, "sev1", "resolved", "2024-03-02", "2024-03-03", 0, 8.0),
                (701, 102, 4, "sev1", "open", "2024-03-04", None, 1, None),
                (702, 105, 4, "sev2", "open", "2024-03-05", None, 0, None),
                (703, 101, 4, "sev2", "resolved", "2024-02-20", "2024-02-22", 1, 36.0),
                (704, 104, 4, "sev3", "resolved", "2024-03-07", "2024-03-07", 0, 4.0),
            ),
            primary_key=("id",),
            foreign_keys=(
                ForeignKeySpec("account_id", "accounts", "id"),
                ForeignKeySpec("assignee_rep_id", "reps", "id"),
            ),
        ),
        TableSpec(
            name="usage_events",
            columns=(
                ColumnSpec("id", "INTEGER"),
                ColumnSpec("account_id", "INTEGER"),
                ColumnSpec("event_type", "TEXT"),
                ColumnSpec("occurred_on", "TEXT"),
                ColumnSpec("quantity", "INTEGER"),
            ),
            rows=(
                (800, 100, "login", "2024-02-02", 12),
                (801, 100, "activation", "2024-02-10", 1),
                (802, 101, "login", "2024-02-14", 5),
                (803, 102, "login", "2024-02-20", 2),
                (804, 105, "activation", "2024-03-02", 1),
            ),
            primary_key=("id",),
            foreign_keys=(ForeignKeySpec("account_id", "accounts", "id"),),
        ),
        TableSpec(
            name="nps_responses",
            columns=(
                ColumnSpec("id", "INTEGER"),
                ColumnSpec("account_id", "INTEGER"),
                ColumnSpec("score", "INTEGER"),
                ColumnSpec("responded_on", "TEXT"),
            ),
            rows=(
                (900, 100, 9, "2024-03-02"),
                (901, 101, 7, "2024-03-04"),
                (902, 102, 4, "2024-03-05"),
                (903, 105, 6, "2024-02-25"),
            ),
            primary_key=("id",),
            foreign_keys=(ForeignKeySpec("account_id", "accounts", "id"),),
        ),
        TableSpec(
            name="contacts",
            columns=(
                ColumnSpec("id", "INTEGER"),
                ColumnSpec("account_id", "INTEGER"),
                ColumnSpec("email", "TEXT"),
                ColumnSpec("role", "TEXT"),
                ColumnSpec("created_on", "TEXT"),
            ),
            rows=(
                (1000, 100, "buyer@acme.com", "economic_buyer", "2024-01-05"),
                (1001, 102, "cio@northstar.health", "technical_buyer", "2024-01-10"),
                (1002, 105, "ops@greenfield.energy", "admin", "2024-03-01"),
            ),
            primary_key=("id",),
            foreign_keys=(ForeignKeySpec("account_id", "accounts", "id"),),
        ),
    ),
)


BUSINESS_ANALYTICS_CASES: tuple[PlatformQueryCase, ...] = (
    PlatformQueryCase(
        id="ba001",
        question="List enterprise accounts in EMEA renewing in March 2024 with owner and ARR",
        disposition="route",
        family="renewal_owner_projection",
        difficulty="medium",
        expected_sql=(
            "SELECT accounts.company_name, reps.full_name, accounts.arr "
            "FROM accounts "
            "JOIN regions ON accounts.region_id = regions.id "
            "JOIN reps ON accounts.owner_rep_id = reps.id "
            "WHERE accounts.segment = 'enterprise' "
            "AND regions.name = 'EMEA' "
            "AND accounts.renewal_date >= '2024-03-01' "
            "AND accounts.renewal_date < '2024-04-01' "
            "ORDER BY accounts.arr DESC"
        ),
        reason="Common renewal dashboard question with owner and numeric projection.",
        semsql_expectation="Important practical BI route target.",
        competitor_signal="Tests schema graph, date range, display projection, and ordering.",
    ),
    PlatformQueryCase(
        id="ba002",
        question="Total ARR by region for active customers",
        disposition="route",
        family="grouped_arr_metric",
        difficulty="medium",
        expected_sql=(
            "SELECT regions.name, SUM(accounts.arr) AS total_arr "
            "FROM accounts "
            "JOIN regions ON accounts.region_id = regions.id "
            "WHERE accounts.status = 'active' "
            "GROUP BY regions.name "
            "ORDER BY total_arr DESC"
        ),
        reason="Standard BI grouped metric.",
        semsql_expectation="Should become a core state-machine shape.",
        competitor_signal="Tests metric grouping and dimension selection.",
    ),
    PlatformQueryCase(
        id="ba003",
        question="Top 3 reps by open pipeline closing in Q2 2024",
        disposition="route",
        family="crm_pipeline_topk",
        difficulty="hard",
        expected_sql=(
            "SELECT reps.full_name, SUM(opportunities.amount) AS open_pipeline "
            "FROM opportunities "
            "JOIN reps ON opportunities.owner_rep_id = reps.id "
            "WHERE opportunities.stage IN ('qualified', 'proposal', 'negotiation') "
            "AND opportunities.close_date >= '2024-04-01' "
            "AND opportunities.close_date < '2024-07-01' "
            "GROUP BY reps.full_name "
            "ORDER BY open_pipeline DESC "
            "LIMIT 3"
        ),
        reason="CRM leaderboard with IN-list stage set and quarter range.",
        semsql_expectation="High-value route target after grouped-metric generalization.",
        competitor_signal="Tests BI synonym mapping for pipeline and quarter.",
    ),
    PlatformQueryCase(
        id="ba004",
        question="How many new leads came from paid search in February 2024?",
        disposition="route",
        family="growth_channel_count",
        difficulty="easy",
        expected_sql=(
            "SELECT COUNT(leads.id) FROM leads "
            "WHERE leads.source_channel = 'paid_search' "
            "AND leads.created_on >= '2024-02-01' "
            "AND leads.created_on < '2024-03-01'"
        ),
        reason="Growth funnel count by channel and month.",
        semsql_expectation="Should route once channel/date evidence is generic.",
        competitor_signal="Tests funnel vocabulary and date normalization.",
    ),
    PlatformQueryCase(
        id="ba005",
        question="Lead count by source channel in February 2024",
        disposition="route",
        family="growth_channel_group_count",
        difficulty="medium",
        expected_sql=(
            "SELECT leads.source_channel, COUNT(leads.id) AS lead_count "
            "FROM leads "
            "WHERE leads.created_on >= '2024-02-01' "
            "AND leads.created_on < '2024-03-01' "
            "GROUP BY leads.source_channel "
            "ORDER BY lead_count DESC"
        ),
        reason="Dashboard bar chart shape.",
        semsql_expectation="Core BI grouped count target.",
        competitor_signal="Tests result-shape and grouped count.",
    ),
    PlatformQueryCase(
        id="ba006",
        question="Converted leads by campaign",
        disposition="route",
        family="campaign_conversion_count",
        difficulty="medium",
        expected_sql=(
            "SELECT campaigns.name, COUNT(leads.id) AS converted_leads "
            "FROM leads "
            "JOIN campaigns ON leads.source_campaign_id = campaigns.id "
            "WHERE leads.status = 'converted' "
            "GROUP BY campaigns.name "
            "ORDER BY converted_leads DESC"
        ),
        reason="Campaign reporting without a ratio.",
        semsql_expectation="Route target before attempting conversion-rate ratios.",
        competitor_signal="Tests campaign attribution join.",
    ),
    PlatformQueryCase(
        id="ba007",
        question="Average sales cycle days for won opportunities by segment",
        disposition="route",
        family="sales_cycle_group_avg",
        difficulty="hard",
        expected_sql=(
            "SELECT accounts.segment, "
            "AVG(julianday(opportunities.close_date) - julianday(opportunities.created_on)) "
            "AS avg_sales_cycle_days "
            "FROM opportunities "
            "JOIN accounts ON opportunities.account_id = accounts.id "
            "WHERE opportunities.stage = 'closed_won' "
            "GROUP BY accounts.segment "
            "ORDER BY avg_sales_cycle_days ASC"
        ),
        reason="Derived duration metric over won deals.",
        semsql_expectation="Stretch route target; needs typed derived metric support.",
        competitor_signal="Tests date arithmetic and metric naming.",
    ),
    PlatformQueryCase(
        id="ba008",
        question="MRR by plan for active subscriptions",
        disposition="route",
        family="subscription_mrr_group",
        difficulty="medium",
        expected_sql=(
            "SELECT subscriptions.plan, SUM(subscriptions.mrr) AS total_mrr "
            "FROM subscriptions "
            "WHERE subscriptions.status = 'active' "
            "GROUP BY subscriptions.plan "
            "ORDER BY total_mrr DESC"
        ),
        reason="Canonical SaaS BI metric.",
        semsql_expectation="Core route target.",
        competitor_signal="Tests metric synonym and grouping.",
    ),
    PlatformQueryCase(
        id="ba009",
        question="Overdue invoice amount by account owner",
        disposition="route",
        family="billing_owner_group_sum",
        difficulty="medium",
        expected_sql=(
            "SELECT reps.full_name, SUM(invoices.amount) AS overdue_amount "
            "FROM invoices "
            "JOIN accounts ON invoices.account_id = accounts.id "
            "JOIN reps ON accounts.owner_rep_id = reps.id "
            "WHERE invoices.status = 'overdue' "
            "GROUP BY reps.full_name "
            "ORDER BY overdue_amount DESC"
        ),
        reason="Operational finance and customer-success action list.",
        semsql_expectation="Important route target.",
        competitor_signal="Tests owner role and billing fact joins.",
    ),
    PlatformQueryCase(
        id="ba010",
        question="Customers with open severity 1 tickets and renewals before April 2024",
        disposition="route",
        family="support_renewal_intersection",
        difficulty="hard",
        expected_sql=(
            "SELECT DISTINCT accounts.company_name, accounts.renewal_date "
            "FROM accounts "
            "JOIN tickets ON tickets.account_id = accounts.id "
            "WHERE tickets.severity = 'sev1' "
            "AND tickets.status = 'open' "
            "AND accounts.renewal_date < '2024-04-01' "
            "ORDER BY accounts.renewal_date"
        ),
        reason="Customer-success risk list joining support and renewal context.",
        semsql_expectation="High-value state-machine target.",
        competitor_signal="Tests fact intersection and month boundary.",
    ),
    PlatformQueryCase(
        id="ba011",
        question="Average NPS score by industry in March 2024",
        disposition="route",
        family="nps_group_avg",
        difficulty="medium",
        expected_sql=(
            "SELECT accounts.industry, AVG(nps_responses.score) AS avg_nps "
            "FROM nps_responses "
            "JOIN accounts ON nps_responses.account_id = accounts.id "
            "WHERE nps_responses.responded_on >= '2024-03-01' "
            "AND nps_responses.responded_on < '2024-04-01' "
            "GROUP BY accounts.industry "
            "ORDER BY avg_nps DESC"
        ),
        reason="Customer analytics metric with date filter.",
        semsql_expectation="Route target after survey metric vocabulary.",
        competitor_signal="Tests non-revenue metric grounding.",
    ),
    PlatformQueryCase(
        id="ba012",
        question="List churned accounts in Q1 2024 with segment and ARR",
        disposition="route",
        family="churn_list_projection",
        difficulty="medium",
        expected_sql=(
            "SELECT accounts.company_name, accounts.segment, accounts.arr "
            "FROM accounts "
            "JOIN subscriptions ON subscriptions.account_id = accounts.id "
            "WHERE subscriptions.status = 'cancelled' "
            "AND subscriptions.ended_on >= '2024-01-01' "
            "AND subscriptions.ended_on < '2024-04-01' "
            "ORDER BY accounts.arr DESC"
        ),
        reason="Churn operations list with quarter range.",
        semsql_expectation="Route target once quarter parsing is supported.",
        competitor_signal="Tests lifecycle vocabulary.",
    ),
    PlatformQueryCase(
        id="ba013",
        question="Find customer with domain acme.com",
        disposition="route",
        family="structured_domain_lookup",
        difficulty="easy",
        expected_sql=(
            "SELECT accounts.company_name FROM accounts "
            "WHERE accounts.domain = 'acme.com'"
        ),
        reason="Structured literal lookup.",
        semsql_expectation="Should be a current or near-current strength.",
        competitor_signal="Tests exact literal preservation.",
    ),
    PlatformQueryCase(
        id="ba014",
        question="Accounts owned by inactive reps",
        disposition="route",
        family="inactive_owner_filter",
        difficulty="medium",
        expected_sql=(
            "SELECT accounts.company_name, reps.full_name "
            "FROM accounts "
            "JOIN reps ON accounts.owner_rep_id = reps.id "
            "WHERE reps.active = 0 "
            "ORDER BY accounts.company_name"
        ),
        reason="Operational hygiene question.",
        semsql_expectation="Route target.",
        competitor_signal="Tests boolean normalization over owner join.",
    ),
    PlatformQueryCase(
        id="ba015",
        question="Daily signups by source channel in February 2024",
        disposition="route",
        family="time_series_group_count",
        difficulty="medium",
        expected_sql=(
            "SELECT leads.created_on, leads.source_channel, COUNT(leads.id) AS signups "
            "FROM leads "
            "WHERE leads.created_on >= '2024-02-01' "
            "AND leads.created_on < '2024-03-01' "
            "GROUP BY leads.created_on, leads.source_channel "
            "ORDER BY leads.created_on, leads.source_channel"
        ),
        reason="Chart-friendly time-series grouped count.",
        semsql_expectation="Route target plus result-shape target.",
        competitor_signal="Tests chart/table auto-output plumbing.",
    ),
    PlatformQueryCase(
        id="ba016",
        question="Open opportunities over 50000 by stage",
        disposition="route",
        family="pipeline_stage_group_sum",
        difficulty="medium",
        expected_sql=(
            "SELECT opportunities.stage, COUNT(opportunities.id) AS opportunity_count, "
            "SUM(opportunities.amount) AS pipeline_amount "
            "FROM opportunities "
            "WHERE opportunities.amount > 50000 "
            "AND opportunities.stage IN ('qualified', 'proposal', 'negotiation') "
            "GROUP BY opportunities.stage "
            "ORDER BY pipeline_amount DESC"
        ),
        reason="Sales ops pipeline distribution.",
        semsql_expectation="Route target after multi-projection grouped metrics.",
        competitor_signal="Tests multiple measures in one grouped result.",
    ),
    PlatformQueryCase(
        id="ba017",
        question="Resolved ticket count by support rep in March 2024",
        disposition="route",
        family="support_rep_group_count",
        difficulty="medium",
        expected_sql=(
            "SELECT reps.full_name, COUNT(tickets.id) AS resolved_tickets "
            "FROM tickets "
            "JOIN reps ON tickets.assignee_rep_id = reps.id "
            "WHERE tickets.status = 'resolved' "
            "AND tickets.resolved_on >= '2024-03-01' "
            "AND tickets.resolved_on < '2024-04-01' "
            "GROUP BY reps.full_name "
            "ORDER BY resolved_tickets DESC"
        ),
        reason="Support operations leaderboard.",
        semsql_expectation="Route target.",
        competitor_signal="Tests assignee role over owner role.",
    ),
    PlatformQueryCase(
        id="ba018",
        question="Which active customers have not had a sales activity since March 1 2024?",
        disposition="route",
        family="anti_join_temporal",
        difficulty="hard",
        expected_sql=(
            "SELECT accounts.company_name FROM accounts "
            "WHERE accounts.status = 'active' "
            "AND NOT EXISTS ("
            "SELECT 1 FROM activities "
            "WHERE activities.account_id = accounts.id "
            "AND activities.occurred_on >= '2024-03-01'"
            ") "
            "ORDER BY accounts.company_name"
        ),
        reason="Requires anti-join and temporal recency.",
        semsql_expectation="Route target through a governed anti-join activity frame.",
        competitor_signal="Tests whether systems can express NOT EXISTS safely.",
    ),
    PlatformQueryCase(
        id="ba019",
        question="Lead-to-customer conversion rate by campaign",
        disposition="route",
        family="ratio_by_group",
        difficulty="hard",
        expected_sql=(
            "SELECT campaigns.name, "
            "CAST(SUM(CASE WHEN leads.status = 'converted' THEN 1 ELSE 0 END) AS REAL) "
            "* 100.0 / COUNT(leads.id) AS conversion_rate "
            "FROM leads "
            "JOIN campaigns ON leads.source_campaign_id = campaigns.id "
            "GROUP BY campaigns.name "
            "ORDER BY conversion_rate DESC"
        ),
        reason="Conditional aggregate ratio.",
        semsql_expectation="Route target through a governed conversion-rate metric frame.",
        competitor_signal="Core BI semantic-layer comparison.",
    ),
    PlatformQueryCase(
        id="ba020",
        question="SLA breach rate by segment",
        disposition="route",
        family="ratio_by_joined_dimension",
        difficulty="hard",
        expected_sql=(
            "SELECT accounts.segment, "
            "CAST(SUM(CASE WHEN tickets.sla_breached = 1 THEN 1 ELSE 0 END) AS REAL) "
            "* 100.0 / COUNT(tickets.id) AS sla_breach_rate "
            "FROM tickets "
            "JOIN accounts ON tickets.account_id = accounts.id "
            "GROUP BY accounts.segment "
            "ORDER BY sla_breach_rate DESC"
        ),
        reason="Conditional ratio through a joined dimension.",
        semsql_expectation="Route target through a governed SLA-breach metric frame.",
        competitor_signal="Tests semantic metric catalog and joins.",
    ),
    PlatformQueryCase(
        id="ba021",
        question="Show revenue",
        disposition="clarify",
        family="ambiguous_metric_timeframe",
        difficulty="easy",
        expected_sql=None,
        reason="Revenue could mean ARR, MRR, bookings, paid invoices, or recognized revenue.",
        semsql_expectation="Must ask for metric and timeframe.",
        competitor_signal="Tests BI metric catalog dependence.",
    ),
    PlatformQueryCase(
        id="ba022",
        question="Show pipeline",
        disposition="clarify",
        family="ambiguous_pipeline_metric",
        difficulty="easy",
        expected_sql=None,
        reason="Pipeline could mean rows, count, amount, open-only, weighted, or by stage.",
        semsql_expectation="Must clarify instead of guessing.",
        competitor_signal="Tests default-metric discipline.",
    ),
    PlatformQueryCase(
        id="ba023",
        question="Which customers are at risk?",
        disposition="clarify",
        family="undefined_business_metric",
        difficulty="medium",
        expected_sql=None,
        reason="'At risk' needs a configured formula such as health score threshold plus support or renewal context.",
        semsql_expectation="Must ask for risk definition unless a metric catalog maps it.",
        competitor_signal="Tests semantic-layer governance.",
    ),
    PlatformQueryCase(
        id="ba024",
        question="Export all contact emails for enterprise accounts",
        disposition="reject",
        family="pii_row_dump",
        difficulty="easy",
        expected_sql=None,
        reason="Requests broad PII export.",
        semsql_expectation="Must reject unless an explicit governed export workflow is active.",
        competitor_signal="Tests privacy posture.",
    ),
    PlatformQueryCase(
        id="ba025",
        question="Create follow-up tasks for stale opportunities",
        disposition="reject",
        family="unsafe_action",
        difficulty="easy",
        expected_sql=None,
        reason="Requests a write-side effect, not read-only SQL.",
        semsql_expectation="Must reject.",
        competitor_signal="Tests action boundary.",
    ),
    PlatformQueryCase(
        id="ba026",
        question="Why did ARR drop last week?",
        disposition="reject",
        family="causal_analysis",
        difficulty="hard",
        expected_sql=None,
        reason="Causal analysis is not a single grounded SQL lookup.",
        semsql_expectation="Should route to analysis workflow, not SQL generation.",
        competitor_signal="Tests analytical-mode separation.",
    ),
)


PLATFORM_CASES: tuple[PlatformQueryCase, ...] = (
    PlatformQueryCase(
        id="pq001",
        question="List active enterprise accounts in EMEA with their owner",
        disposition="route",
        family="multi_join_filter_projection",
        difficulty="medium",
        expected_sql=(
            "SELECT accounts.company_name, agents.full_name "
            "FROM accounts "
            "JOIN plans ON accounts.plan_id = plans.id "
            "JOIN regions ON accounts.region_id = regions.id "
            "JOIN agents ON accounts.owner_agent_id = agents.id "
            "WHERE accounts.status = 'active' "
            "AND plans.tier = 'enterprise' "
            "AND regions.name = 'EMEA' "
            "ORDER BY accounts.company_name"
        ),
        reason="Grounded labels, enum filters, and three FK joins.",
        semsql_expectation="Should be a target strength for QueryFrame routing.",
        competitor_signal="Tests schema linking plus join planning.",
    ),
    PlatformQueryCase(
        id="pq002",
        question="How many active accounts signed up in February 2024?",
        disposition="route",
        family="date_range_count",
        difficulty="medium",
        expected_sql=(
            "SELECT COUNT(accounts.id) FROM accounts "
            "WHERE accounts.status = 'active' "
            "AND accounts.signup_date >= '2024-02-01' "
            "AND accounts.signup_date < '2024-03-01'"
        ),
        reason="Common date bucket with status filter.",
        semsql_expectation="Should route once date-bucket parsing is generalized.",
        competitor_signal="Tests temporal phrase normalization.",
    ),
    PlatformQueryCase(
        id="pq003",
        question="Total paid invoice amount for Acme Cloud in February 2024",
        disposition="route",
        family="entity_value_join_aggregate_date",
        difficulty="medium",
        expected_sql=(
            "SELECT SUM(invoices.amount) FROM invoices "
            "JOIN accounts ON invoices.account_id = accounts.id "
            "WHERE accounts.company_name = 'Acme Cloud' "
            "AND invoices.status = 'paid' "
            "AND invoices.issued_on >= '2024-02-01' "
            "AND invoices.issued_on < '2024-03-01'"
        ),
        reason="Entity value, status value, date range, aggregate.",
        semsql_expectation="Current value-grounded path should be close.",
        competitor_signal="Tests table descriptions plus sample/entity lookup.",
    ),
    PlatformQueryCase(
        id="pq004",
        question="Which support agent resolved the most high priority tickets?",
        disposition="route",
        family="topk_group_count",
        difficulty="medium",
        expected_sql=(
            "SELECT agents.full_name, COUNT(tickets.id) AS resolved_ticket_count "
            "FROM tickets "
            "JOIN agents ON tickets.assignee_id = agents.id "
            "WHERE tickets.priority = 'high' "
            "AND tickets.status = 'resolved' "
            "GROUP BY agents.full_name "
            "ORDER BY resolved_ticket_count DESC "
            "LIMIT 1"
        ),
        reason="Top-k grouped count over one FK join.",
        semsql_expectation="Target strength, similar to current top-k canary but richer.",
        competitor_signal="Tests aggregation alias and ORDER BY.",
    ),
    PlatformQueryCase(
        id="pq005",
        question="Average resolution hours for enterprise accounts by support agent",
        disposition="route",
        family="multi_join_group_avg",
        difficulty="hard",
        expected_sql=(
            "SELECT agents.full_name, AVG(tickets.resolution_hours) AS avg_resolution_hours "
            "FROM tickets "
            "JOIN accounts ON tickets.account_id = accounts.id "
            "JOIN plans ON accounts.plan_id = plans.id "
            "JOIN agents ON tickets.assignee_id = agents.id "
            "WHERE plans.tier = 'enterprise' "
            "AND tickets.resolution_hours IS NOT NULL "
            "GROUP BY agents.full_name "
            "ORDER BY avg_resolution_hours ASC"
        ),
        reason="Three joins, numeric measure, null guard, grouped aggregate.",
        semsql_expectation="Important next QueryFrame hardening target.",
        competitor_signal="Agentic tools may decompose this well if schema descriptions are present.",
    ),
    PlatformQueryCase(
        id="pq006",
        question="Show accounts with overdue invoices and open tickets",
        disposition="route",
        family="two_fact_intersection",
        difficulty="hard",
        expected_sql=(
            "SELECT DISTINCT accounts.company_name "
            "FROM accounts "
            "JOIN invoices ON invoices.account_id = accounts.id "
            "JOIN tickets ON tickets.account_id = accounts.id "
            "WHERE invoices.status = 'overdue' "
            "AND tickets.status = 'open' "
            "ORDER BY accounts.company_name"
        ),
        reason="Intersects two fact tables through the account entity.",
        semsql_expectation="Good discriminator for graph traversal and duplicate control.",
        competitor_signal="Tests whether the model invents a direct invoice-ticket join.",
    ),
    PlatformQueryCase(
        id="pq007",
        question="Which active accounts have no login events after March 1 2024?",
        disposition="route",
        family="anti_join_temporal",
        difficulty="hard",
        expected_sql=(
            "SELECT accounts.company_name FROM accounts "
            "WHERE accounts.status = 'active' "
            "AND NOT EXISTS ("
            "SELECT 1 FROM events "
            "WHERE events.account_id = accounts.id "
            "AND events.event_type = 'login' "
            "AND events.occurred_on > '2024-03-01'"
            ") "
            "ORDER BY accounts.company_name"
        ),
        reason="Requires anti-join and temporal event reasoning.",
        semsql_expectation="Route target once a governed NOT EXISTS anti-join frame is available.",
        competitor_signal="DB-GPT-style agents may attempt a subquery.",
    ),
    PlatformQueryCase(
        id="pq008",
        question="What percentage of active accounts are enterprise accounts?",
        disposition="known_gap",
        family="ratio_conditional_aggregate",
        difficulty="hard",
        expected_sql=(
            "SELECT "
            "CAST(SUM(CASE WHEN plans.tier = 'enterprise' THEN 1 ELSE 0 END) AS REAL) "
            "* 100.0 / COUNT(accounts.id) "
            "FROM accounts "
            "JOIN plans ON accounts.plan_id = plans.id "
            "WHERE accounts.status = 'active'"
        ),
        reason="Requires conditional aggregate ratio.",
        semsql_expectation="Known harder shape; should not be accepted unless renderer supports it.",
        competitor_signal="Good test for analytical SQL generation.",
    ),
    PlatformQueryCase(
        id="pq009",
        question="Compare February paid invoice totals for EMEA and APAC",
        disposition="route",
        family="grouped_metric_comparison",
        difficulty="hard",
        expected_sql=(
            "SELECT regions.name, SUM(invoices.amount) AS paid_amount "
            "FROM invoices "
            "JOIN accounts ON invoices.account_id = accounts.id "
            "JOIN regions ON accounts.region_id = regions.id "
            "WHERE invoices.status = 'paid' "
            "AND invoices.issued_on >= '2024-02-01' "
            "AND invoices.issued_on < '2024-03-01' "
            "AND regions.name IN ('EMEA', 'APAC') "
            "GROUP BY regions.name "
            "ORDER BY regions.name"
        ),
        reason="Grouped comparison with IN-list and date range.",
        semsql_expectation="Useful stretch target beyond the compact canary.",
        competitor_signal="Tests natural comparison phrasing.",
    ),
    PlatformQueryCase(
        id="pq010",
        question="List accounts owned by inactive agents",
        disposition="route",
        family="boolean_join_filter",
        difficulty="medium",
        expected_sql=(
            "SELECT accounts.company_name, agents.full_name "
            "FROM accounts "
            "JOIN agents ON accounts.owner_agent_id = agents.id "
            "WHERE agents.active = 0 "
            "ORDER BY accounts.company_name"
        ),
        reason="Boolean filter over joined owner table.",
        semsql_expectation="Should route if boolean vocabulary is extracted.",
        competitor_signal="Tests boolean normalization.",
    ),
    PlatformQueryCase(
        id="pq011",
        question="Show status",
        disposition="clarify",
        family="ambiguous_projection",
        difficulty="easy",
        expected_sql=None,
        reason="Several tables have status; the user did not name an entity.",
        semsql_expectation="Should fail closed with actionable ambiguity.",
        competitor_signal="Tests whether a platform guesses a random status column.",
    ),
    PlatformQueryCase(
        id="pq012",
        question="Show open things",
        disposition="clarify",
        family="ambiguous_entity",
        difficulty="easy",
        expected_sql=None,
        reason="'open' can refer to invoices or tickets.",
        semsql_expectation="Should ask whether invoices or tickets are intended.",
        competitor_signal="Tests clarification behavior.",
    ),
    PlatformQueryCase(
        id="pq013",
        question="Why did revenue drop in March?",
        disposition="reject",
        family="causal_analysis",
        difficulty="hard",
        expected_sql=None,
        reason="Causal analysis is not a single grounded SQL lookup.",
        semsql_expectation="Should reject or route to an analysis workflow, not generate SQL.",
        competitor_signal="Agentic tools may summarize but need explicit analysis mode.",
    ),
    PlatformQueryCase(
        id="pq014",
        question="Email all accounts with overdue invoices",
        disposition="reject",
        family="unsafe_action",
        difficulty="easy",
        expected_sql=None,
        reason="Requests an external side effect, not read-only SQL.",
        semsql_expectation="Must reject.",
        competitor_signal="Tests safety boundary.",
    ),
    PlatformQueryCase(
        id="pq015",
        question="List every ticket with all columns",
        disposition="reject",
        family="row_dump",
        difficulty="easy",
        expected_sql=None,
        reason="Broad row dump is not acceptable as a default production route.",
        semsql_expectation="Should fail closed unless explicitly allowed.",
        competitor_signal="Tests privacy/data-minimization posture.",
    ),
    PlatformQueryCase(
        id="pq016",
        question="Which customer is healthiest?",
        disposition="clarify",
        family="undefined_business_metric",
        difficulty="medium",
        expected_sql=None,
        reason="'Healthiest' is not mapped to a field or formula.",
        semsql_expectation="Should ask for the health metric definition.",
        competitor_signal="Tests semantic metric catalog dependence.",
    ),
    PlatformQueryCase(
        id="pq017",
        question="Find account ACME-001",
        disposition="route",
        family="structured_identifier",
        difficulty="easy",
        expected_sql=(
            "SELECT accounts.company_name FROM accounts "
            "WHERE accounts.external_code = 'ACME-001'"
        ),
        reason="Structured alphanumeric literal tied to compatible identifier field.",
        semsql_expectation="Should be a current strength.",
        competitor_signal="Tests literal preservation.",
    ),
    PlatformQueryCase(
        id="pq018",
        question="Accounts with a cancellation event",
        disposition="route",
        family="event_filter_join",
        difficulty="medium",
        expected_sql=(
            "SELECT DISTINCT accounts.company_name "
            "FROM accounts "
            "JOIN events ON events.account_id = accounts.id "
            "WHERE events.event_type = 'cancelled' "
            "ORDER BY accounts.company_name"
        ),
        reason="Event-table value grounding and de-duplication.",
        semsql_expectation="Good next target for typed event facts.",
        competitor_signal="Tests event/value mapping.",
    ),
)
