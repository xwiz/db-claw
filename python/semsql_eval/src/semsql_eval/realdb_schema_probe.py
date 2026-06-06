"""Safe schema-first probes for existing MySQL/MariaDB/Postgres databases.

This module is intentionally narrower than the generated QueryFrame canaries:
it can point at a real database server, so sample values stay disabled by
default and the required safety contract executes only exact
``SELECT COUNT(*) FROM <table>`` routes. Optional analytics probes exercise
richer governed aggregate shapes, including opt-in bounded non-PII sample-backed
value filters, but only when the generated SQL matches the expected
table/field/value shape exactly. Result values are discarded immediately.
"""

from __future__ import annotations

import json
import os
import random
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from importlib import import_module
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

from .cascade_runner import build_graph_for_db_url, run_cascade_query

SYSTEM_DATABASES = {"information_schema", "mysql", "performance_schema", "sys"}
POSTGRES_SYSTEM_DATABASES = {"postgres", "template0", "template1"}
POSTGRES_SYSTEM_SCHEMAS = {"information_schema", "pg_catalog"}
COUNT_ONLY_RE = re.compile(
    r"^\s*SELECT\s+COUNT\s*\(\s*\*\s*\)\s+FROM\s+`?([A-Za-z0-9_]+)`?\s*$",
    re.IGNORECASE,
)
COUNT_DATE_RE = re.compile(
    r"^\s*SELECT\s+COUNT\s*\(\s*\*\s*\)\s+FROM\s+`?([A-Za-z0-9_]+)`?\s+"
    r"WHERE\s+`?([A-Za-z0-9_]+)`?\.`?([A-Za-z0-9_]+)`?\s*=\s*"
    r"'(\d{4}-\d{2}-\d{2})'\s*$",
    re.IGNORECASE,
)
GROUP_COUNT_RE = re.compile(
    r"^\s*SELECT\s+`?([A-Za-z0-9_]+)`?\.`?([A-Za-z0-9_]+)`?\s*,\s*"
    r"COUNT\s*\(\s*(?:`?[A-Za-z0-9_]+`?\.`?[A-Za-z0-9_]+`?|\*)\s*\)"
    r"(?:\s+AS\s+`?[A-Za-z0-9_]+`?)?\s+FROM\s+`?([A-Za-z0-9_]+)`?\s+"
    r"GROUP\s+BY\s+`?([A-Za-z0-9_]+)`?\.`?([A-Za-z0-9_]+)`?"
    r"(?:\s+ORDER\s+BY\s+.+)?\s*$",
    re.IGNORECASE,
)
AVG_RE = re.compile(
    r"^\s*SELECT\s+AVG\s*\(\s*`?([A-Za-z0-9_]+)`?\.`?([A-Za-z0-9_]+)`?\s*\)"
    r"(?:\s+AS\s+`?[A-Za-z0-9_]+`?)?\s+FROM\s+`?([A-Za-z0-9_]+)`?\s*$",
    re.IGNORECASE,
)
AVG_NOT_NULL_RE = re.compile(
    r"^\s*SELECT\s+AVG\s*\(\s*`?([A-Za-z0-9_]+)`?\.`?([A-Za-z0-9_]+)`?\s*\)"
    r"(?:\s+AS\s+`?[A-Za-z0-9_]+`?)?\s+FROM\s+`?([A-Za-z0-9_]+)`?\s+"
    r"WHERE\s+`?([A-Za-z0-9_]+)`?\.`?([A-Za-z0-9_]+)`?\s+IS\s+NOT\s+NULL\s*$",
    re.IGNORECASE,
)
SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
SHARD_ANCHORS = {
    "accounts",
    "clients",
    "customers",
    "employees",
    "members",
    "organizations",
    "organisations",
    "tenants",
    "users",
}
NUMERIC_TYPES = {
    "bigint",
    "decimal",
    "double precision",
    "double",
    "float",
    "int",
    "integer",
    "money",
    "mediumint",
    "numeric",
    "real",
    "smallint",
}
DATE_TYPES = {
    "date",
    "datetime",
    "timestamp",
    "timestamp without time zone",
    "timestamp with time zone",
}
TEXT_TYPES = {"char", "character varying", "enum", "text", "varchar"}
GROUPABLE_COLUMN_NAMES = {
    "active",
    "algorithm",
    "event",
    "form_signup",
    "guard_name",
    "log_name",
    "method",
    "model",
    "socialite_signup",
    "status",
    "super_admin",
    "type",
}
BOOLEAN_COLUMN_NAMES = {
    "active",
    "approved",
    "archived",
    "blocked",
    "closed",
    "completed",
    "confirmed",
    "converted",
    "deleted",
    "disabled",
    "enabled",
    "featured",
    "internal",
    "locked",
    "opened",
    "processed",
    "published",
    "recurring",
    "resolved",
    "sent",
    "shared",
    "verified",
}
BOOLEAN_COLUMN_PREFIXES = (
    "auto_",
    "can_",
    "enable_",
    "has_",
    "is_",
    "requires_",
    "should_",
)
BOOLEAN_COLUMN_SUFFIXES = (
    "_enabled",
    "_locked",
    "_verified",
    "_approved",
    "_active",
    "_public",
    "_private",
)
VALUE_FILTER_FIELD_TOKENS = {
    "category",
    "channel",
    "currency",
    "event",
    "kind",
    "method",
    "platform",
    "priority",
    "provider",
    "role",
    "segment",
    "severity",
    "source",
    "state",
    "status",
    "tier",
    "type",
}
NON_BOOLEAN_TINYINT_NAMES = {
    "attempt",
    "attempts",
    "count",
    "digits",
    "level",
    "order",
    "priority",
    "retry",
    "score",
    "seconds",
    "type",
    "weight",
    "window",
}


@dataclass(frozen=True)
class RealDbTable:
    database: str
    table: str

    @property
    def label(self) -> str:
        return humanize_identifier(self.table)

    @property
    def sensitive(self) -> bool:
        return name_looks_sensitive(self.table)


@dataclass(frozen=True)
class RealDbColumn:
    database: str
    table: str
    column: str
    data_type: str
    column_key: str
    is_nullable: str

    @property
    def label(self) -> str:
        return humanize_identifier(self.column)

    @property
    def sensitive(self) -> bool:
        return name_looks_sensitive(self.table) or name_looks_sensitive(self.column)

    @property
    def is_date_like(self) -> bool:
        return self.data_type.lower() in DATE_TYPES

    @property
    def is_metric_like(self) -> bool:
        lower = self.column.lower()
        if self.sensitive or self.column_key.upper() == "PRI":
            return False
        if self.is_date_like:
            return False
        if lower == "id" or lower.endswith("_id") or lower.endswith("_uuid"):
            return False
        if lower.endswith(("_at", "_on", "_date", "_time", "_by")):
            return False
        if lower in {
            "approved_by",
            "assigned_to",
            "created",
            "created_by",
            "deleted_by",
            "generated_by",
            "modified_by",
            "owner",
            "updated_by",
        }:
            return False
        if lower in {"retry"} or lower.endswith("_count"):
            return True
        return self.data_type.lower() in NUMERIC_TYPES and self.data_type.lower() != "tinyint"

    @property
    def is_groupable_like(self) -> bool:
        lower = self.column.lower()
        if self.sensitive or self.column_key.upper() == "PRI":
            return False
        if lower == "id" or lower.endswith("_id") or lower.endswith("_uuid"):
            return False
        return lower in GROUPABLE_COLUMN_NAMES or self.data_type.lower() == "enum"

    @property
    def is_boolean_rate_like(self) -> bool:
        lower = self.column.lower()
        if self.sensitive or self.column_key.upper() == "PRI":
            return False
        if lower == "id" or lower.endswith(("_id", "_uuid")):
            return False
        if lower in NON_BOOLEAN_TINYINT_NAMES:
            return False
        if lower.endswith(("_count", "_score", "_level", "_order", "_priority")):
            return False
        if self.data_type.lower() not in {"tinyint", "bool", "boolean"}:
            return False
        return (
            lower in BOOLEAN_COLUMN_NAMES
            or lower.startswith(BOOLEAN_COLUMN_PREFIXES)
            or lower.endswith(BOOLEAN_COLUMN_SUFFIXES)
        )


@dataclass(frozen=True)
class RealDbRelationship:
    database: str
    table: str
    column: str
    referenced_table: str
    referenced_column: str


@dataclass(frozen=True)
class SqlShape:
    kind: str
    table: str
    field: str | None = None
    literal: str | None = None


def run_mysql_realdb_schema_probe(
    *,
    out_dir: Path,
    semsql_bin: Path,
    db_url: str | None = None,
    database: str | None = None,
    seed: int = 20260601,
    sample_size: int = 10,
    unsafe_prompt_count: int = 2,
    analytics_probe_count: int = 0,
    graph_cache_dir: Path | None = None,
    query_timeout_seconds: int = 30,
    extract_timeout_seconds: int = 60,
    exec_timeout_seconds: float = 10.0,
    include_generated: bool = False,
) -> dict[str, Any]:
    """Run a schema-only, count-only probe against a real MySQL/MariaDB DB."""
    resolved_url = db_url or os.environ.get("SEMSQL_MYSQL_PROBE_URL")
    if not resolved_url:
        return _skipped_report(
            reason="missing_db_url",
            detail="pass --db-url or set SEMSQL_MYSQL_PROBE_URL",
            out_dir=out_dir,
            seed=seed,
        )
    try:
        pymysql = import_module("pymysql")
    except ImportError:
        return _skipped_report(
            reason="missing_pymysql",
            detail="run with `uv run --extra db ...` or install pymysql",
            out_dir=out_dir,
            seed=seed,
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    selected_database = database or _database_from_url(resolved_url)
    try:
        conn = _mysql_connect(pymysql, resolved_url, database=None, autocommit=False)
        try:
            if selected_database is None:
                selected_database = _select_random_database(
                    conn,
                    seed=seed,
                    include_generated=include_generated,
                )
            tables = _list_tables(conn, selected_database)
            columns = _list_columns(conn, selected_database)
        finally:
            conn.close()
    except Exception as error:
        return _error_report(
            out_dir=out_dir,
            seed=seed,
            database=selected_database,
            error=error,
        )

    safe_tables = [table for table in tables if SAFE_IDENTIFIER_RE.match(table.table)]
    skipped_tables = [table.table for table in tables if table not in safe_tables]
    ambiguous_physical_tables = _ambiguous_physical_family_tables(safe_tables)
    routable_tables = [
        table
        for table in safe_tables
        if table.table.lower() not in ambiguous_physical_tables
    ]
    count_tables = _select_count_tables(
        routable_tables,
        seed=seed,
        sample_size=sample_size,
    )
    unsafe_tables = [table for table in safe_tables if table.sensitive][
        :unsafe_prompt_count
    ]
    ambiguous_probe_tables = [
        table for table in safe_tables if table.table.lower() in ambiguous_physical_tables
    ][:unsafe_prompt_count]
    questions = [
        {
            "question": f"how many {table.label}",
            "expected_table": table.table,
            "unsafe_projection_probe": False,
        }
        for table in count_tables
    ]
    questions.extend(
        {
            "question": f"list {table.label}",
            "expected_table": None,
            "unsafe_projection_probe": True,
        }
        for table in unsafe_tables
    )
    questions.extend(
        {
            "question": f"how many {table.label}",
            "expected_table": None,
            "expected_kind": "table_count",
            "expected_reject_probe": True,
            "unsafe_projection_probe": False,
        }
        for table in ambiguous_probe_tables
    )
    questions.extend(
        _select_analytics_questions(
            routable_tables,
            [
                column
                for column in columns
                if column.table.lower() not in ambiguous_physical_tables
            ],
            seed=seed,
            probe_count=analytics_probe_count,
        )
    )

    graph_root = graph_cache_dir or (out_dir / "graphs")
    graph_root.mkdir(parents=True, exist_ok=True)
    graph_path = graph_root / f"{selected_database}.schemaonly.semsql"
    if graph_path.exists():
        graph_path.unlink()
    database_url = _mysql_url_with_database(resolved_url, selected_database)
    try:
        build_graph_for_db_url(
            semsql_bin,
            database_url,
            graph_path,
            path_arg=out_dir,
            timeout_seconds=extract_timeout_seconds,
            sample_values=False,
        )
    except Exception as error:
        return _error_report(
            out_dir=out_dir,
            seed=seed,
            database=selected_database,
            error=error,
        )

    sample_value_rows = _graph_sample_value_count(graph_path)
    records: list[dict[str, Any]] = []
    try:
        conn = _mysql_connect(
            pymysql,
            resolved_url,
            database=selected_database,
            autocommit=False,
        )
        try:
            for index, question in enumerate(questions, start=1):
                records.append(
                    _run_probe_question(
                        index=index,
                        question=question,
                        semsql_bin=semsql_bin,
                        graph_path=graph_path,
                        conn=conn,
                        query_timeout_seconds=query_timeout_seconds,
                        exec_timeout_seconds=exec_timeout_seconds,
                    )
                )
        finally:
            conn.close()
    except Exception as error:
        return _error_report(
            out_dir=out_dir,
            seed=seed,
            database=selected_database,
            error=error,
        )

    summary = _summarize_records(records, sample_value_rows=sample_value_rows)
    high_risk = name_looks_sensitive(selected_database) or any(
        table.sensitive for table in safe_tables
    )
    status = "pass" if summary["pass"] else "fail"
    if (
        summary["pass"]
        and summary.get("analytics_questions", 0) > summary.get("analytics_ok", 0)
    ):
        status = "review"
    return {
        "schema_version": 1,
        "engine": "mysql",
        "status": status,
        "seed": seed,
        "database": selected_database,
        "database_url_redacted": redact_db_url(database_url),
        "out_dir": str(out_dir),
        "graph": str(graph_path),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "safety_mode": (
            "schema-only extraction; required count-only execution; optional "
            "governed analytics execution; no result values retained"
        ),
        "high_risk_schema": high_risk,
        "sensitive_tables": [table.table for table in safe_tables if table.sensitive],
        "skipped_unsafe_identifier_tables": skipped_tables,
        "ambiguous_physical_family_tables": sorted(ambiguous_physical_tables),
        "summary": summary,
        "records": records,
    }


def run_mysql_realdb_schema_probe_suite(
    *,
    out_dir: Path,
    semsql_bin: Path,
    db_url: str | None = None,
    database: str | None = None,
    seeds: list[int] | tuple[int, ...] = (20260601, 20260602, 20260603),
    sample_size: int = 10,
    unsafe_prompt_count: int = 2,
    analytics_probe_count: int = 0,
    graph_cache_dir: Path | None = None,
    query_timeout_seconds: int = 30,
    extract_timeout_seconds: int = 60,
    exec_timeout_seconds: float = 10.0,
    include_generated: bool = False,
) -> dict[str, Any]:
    """Run the safe schema-only probe across several seeded DB draws."""
    out_dir.mkdir(parents=True, exist_ok=True)
    runs: list[dict[str, Any]] = []
    for seed in seeds:
        run_out_dir = out_dir / f"seed-{seed}"
        run_graph_cache_dir = (
            graph_cache_dir / f"seed-{seed}" if graph_cache_dir is not None else None
        )
        runs.append(
            run_mysql_realdb_schema_probe(
                out_dir=run_out_dir,
                semsql_bin=semsql_bin,
                db_url=db_url,
                database=database,
                seed=seed,
                sample_size=sample_size,
                unsafe_prompt_count=unsafe_prompt_count,
                analytics_probe_count=analytics_probe_count,
                graph_cache_dir=run_graph_cache_dir,
                query_timeout_seconds=query_timeout_seconds,
                extract_timeout_seconds=extract_timeout_seconds,
                exec_timeout_seconds=exec_timeout_seconds,
                include_generated=include_generated,
            )
        )
    summary = _summarize_suite_runs(runs)
    status = "skipped" if summary["skipped"] else "pass" if summary["pass"] else "fail"
    if (
        status == "pass"
        and summary.get("analytics_questions", 0) > summary.get("analytics_ok", 0)
    ):
        status = "review"
    return {
        "schema_version": 1,
        "engine": "mysql",
        "status": status,
        "seeds": list(seeds),
        "database": database,
        "database_url_redacted": redact_db_url(db_url) if db_url else None,
        "out_dir": str(out_dir),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "safety_mode": (
            "schema-only extraction; required count-only execution; optional "
            "governed analytics execution; no result values retained"
        ),
        "summary": summary,
        "runs": runs,
    }


def run_postgres_realdb_schema_probe(
    *,
    out_dir: Path,
    semsql_bin: Path,
    db_url: str | None = None,
    database: str | None = None,
    seed: int = 20260601,
    sample_size: int = 10,
    unsafe_prompt_count: int = 2,
    analytics_probe_count: int = 0,
    graph_cache_dir: Path | None = None,
    query_timeout_seconds: int = 30,
    extract_timeout_seconds: int = 60,
    exec_timeout_seconds: float = 10.0,
    include_generated: bool = False,
) -> dict[str, Any]:
    """Run a schema-only, count-only probe against a real Postgres DB."""
    resolved_url = db_url or os.environ.get("SEMSQL_POSTGRES_PROBE_URL")
    if not resolved_url:
        return _skipped_report(
            reason="missing_db_url",
            detail="pass --db-url or set SEMSQL_POSTGRES_PROBE_URL",
            out_dir=out_dir,
            seed=seed,
            engine="postgres",
        )
    try:
        pg_driver = _import_postgres_driver()
    except ImportError:
        return _skipped_report(
            reason="missing_postgres_driver",
            detail="run with `uv run --extra db ...` or install psycopg/psycopg2",
            out_dir=out_dir,
            seed=seed,
            engine="postgres",
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    selected_database = database or _database_from_url(resolved_url)
    try:
        conn = _postgres_connect(pg_driver, resolved_url, database=selected_database)
        try:
            if selected_database is None:
                selected_database = _postgres_current_database(conn)
            if not include_generated and selected_database.lower().startswith(
                ("semsql_", "test_")
            ):
                raise RuntimeError(
                    "selected Postgres database looks generated; pass --include-generated to probe it"
                )
            tables = _list_postgres_tables(conn, selected_database)
            columns = _list_postgres_columns(conn, selected_database)
        finally:
            conn.close()
    except Exception as error:
        return _error_report(
            out_dir=out_dir,
            seed=seed,
            database=selected_database,
            error=error,
            engine="postgres",
        )

    safe_tables = [table for table in tables if SAFE_IDENTIFIER_RE.match(table.table)]
    skipped_tables = [table.table for table in tables if table not in safe_tables]
    ambiguous_physical_tables = _ambiguous_physical_family_tables(safe_tables)
    routable_tables = [
        table
        for table in safe_tables
        if table.table.lower() not in ambiguous_physical_tables
    ]
    count_tables = _select_count_tables(
        routable_tables,
        seed=seed,
        sample_size=sample_size,
    )
    unsafe_tables = [table for table in safe_tables if table.sensitive][
        :unsafe_prompt_count
    ]
    ambiguous_probe_tables = [
        table for table in safe_tables if table.table.lower() in ambiguous_physical_tables
    ][:unsafe_prompt_count]
    questions = [
        {
            "question": f"how many {table.label}",
            "expected_table": table.table,
            "unsafe_projection_probe": False,
        }
        for table in count_tables
    ]
    questions.extend(
        {
            "question": f"list {table.label}",
            "expected_table": None,
            "unsafe_projection_probe": True,
        }
        for table in unsafe_tables
    )
    questions.extend(
        {
            "question": f"how many {table.label}",
            "expected_table": None,
            "expected_kind": "table_count",
            "expected_reject_probe": True,
            "unsafe_projection_probe": False,
        }
        for table in ambiguous_probe_tables
    )
    questions.extend(
        _select_analytics_questions(
            routable_tables,
            [
                column
                for column in columns
                if column.table.lower() not in ambiguous_physical_tables
            ],
            seed=seed,
            probe_count=analytics_probe_count,
        )
    )

    graph_root = graph_cache_dir or (out_dir / "graphs")
    graph_root.mkdir(parents=True, exist_ok=True)
    graph_path = graph_root / f"{selected_database}.schemaonly.semsql"
    if graph_path.exists():
        graph_path.unlink()
    database_url = _postgres_url_with_database(resolved_url, selected_database)
    try:
        build_graph_for_db_url(
            semsql_bin,
            database_url,
            graph_path,
            path_arg=out_dir,
            timeout_seconds=extract_timeout_seconds,
            sample_values=False,
        )
    except Exception as error:
        return _error_report(
            out_dir=out_dir,
            seed=seed,
            database=selected_database,
            error=error,
            engine="postgres",
        )

    sample_value_rows = _graph_sample_value_count(graph_path)
    records: list[dict[str, Any]] = []
    try:
        conn = _postgres_connect(pg_driver, resolved_url, database=selected_database)
        try:
            for index, question in enumerate(questions, start=1):
                records.append(
                    _run_probe_question(
                        index=index,
                        question=question,
                        semsql_bin=semsql_bin,
                        graph_path=graph_path,
                        conn=conn,
                        query_timeout_seconds=query_timeout_seconds,
                        exec_timeout_seconds=exec_timeout_seconds,
                        dialect="postgres",
                    )
                )
        finally:
            conn.close()
    except Exception as error:
        return _error_report(
            out_dir=out_dir,
            seed=seed,
            database=selected_database,
            error=error,
            engine="postgres",
        )

    summary = _summarize_records(records, sample_value_rows=sample_value_rows)
    high_risk = name_looks_sensitive(selected_database) or any(
        table.sensitive for table in safe_tables
    )
    status = "pass" if summary["pass"] else "fail"
    if (
        summary["pass"]
        and summary.get("analytics_questions", 0) > summary.get("analytics_ok", 0)
    ):
        status = "review"
    return {
        "schema_version": 1,
        "engine": "postgres",
        "status": status,
        "seed": seed,
        "database": selected_database,
        "database_url_redacted": redact_db_url(database_url),
        "out_dir": str(out_dir),
        "graph": str(graph_path),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "safety_mode": (
            "schema-only extraction; required count-only execution; optional "
            "governed analytics execution; no result values retained"
        ),
        "high_risk_schema": high_risk,
        "sensitive_tables": [table.table for table in safe_tables if table.sensitive],
        "skipped_unsafe_identifier_tables": skipped_tables,
        "ambiguous_physical_family_tables": sorted(ambiguous_physical_tables),
        "summary": summary,
        "records": records,
    }


def run_postgres_realdb_schema_probe_suite(
    *,
    out_dir: Path,
    semsql_bin: Path,
    db_url: str | None = None,
    database: str | None = None,
    seeds: list[int] | tuple[int, ...] = (20260601, 20260602, 20260603),
    sample_size: int = 10,
    unsafe_prompt_count: int = 2,
    analytics_probe_count: int = 0,
    graph_cache_dir: Path | None = None,
    query_timeout_seconds: int = 30,
    extract_timeout_seconds: int = 60,
    exec_timeout_seconds: float = 10.0,
    include_generated: bool = False,
) -> dict[str, Any]:
    """Run the safe schema-only probe across several seeded Postgres draws."""
    out_dir.mkdir(parents=True, exist_ok=True)
    runs: list[dict[str, Any]] = []
    for seed in seeds:
        run_out_dir = out_dir / f"seed-{seed}"
        run_graph_cache_dir = (
            graph_cache_dir / f"seed-{seed}" if graph_cache_dir is not None else None
        )
        runs.append(
            run_postgres_realdb_schema_probe(
                out_dir=run_out_dir,
                semsql_bin=semsql_bin,
                db_url=db_url,
                database=database,
                seed=seed,
                sample_size=sample_size,
                unsafe_prompt_count=unsafe_prompt_count,
                analytics_probe_count=analytics_probe_count,
                graph_cache_dir=run_graph_cache_dir,
                query_timeout_seconds=query_timeout_seconds,
                extract_timeout_seconds=extract_timeout_seconds,
                exec_timeout_seconds=exec_timeout_seconds,
                include_generated=include_generated,
            )
        )
    summary = _summarize_suite_runs(runs)
    status = "skipped" if summary["skipped"] else "pass" if summary["pass"] else "fail"
    if (
        status == "pass"
        and summary.get("analytics_questions", 0) > summary.get("analytics_ok", 0)
    ):
        status = "review"
    return {
        "schema_version": 1,
        "engine": "postgres",
        "status": status,
        "seeds": list(seeds),
        "database": database,
        "database_url_redacted": redact_db_url(db_url) if db_url else None,
        "out_dir": str(out_dir),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "safety_mode": (
            "schema-only extraction; required count-only execution; optional "
            "governed analytics execution; no result values retained"
        ),
        "summary": summary,
        "runs": runs,
    }


def render_mysql_realdb_schema_probe_markdown(report: dict[str, Any]) -> str:
    status = str(report.get("status", "unknown")).upper()
    summary = report["summary"]
    lines = [
        "# Real DB MySQL/MariaDB Schema-Only Probe",
        "",
        f"- status: `{status}`",
        f"- database: `{report.get('database')}`",
        f"- graph: `{report.get('graph')}`",
        f"- high-risk schema: `{report.get('high_risk_schema')}`",
        f"- safety mode: `{report.get('safety_mode')}`",
        "",
    ]
    if summary.get("skipped"):
        lines.extend(
            [
                "## Skip Reason",
                "",
                f"- reason: `{report.get('skip_reason')}`",
                f"- detail: {report.get('skip_detail')}",
                "",
            ]
        )
        return "\n".join(lines)
    if report.get("status") == "error":
        lines.extend(["## Error", "", f"- detail: {report.get('error_detail')}", ""])
        return "\n".join(lines)
    lines.extend(
        [
            "## Summary",
            "",
            f"- questions: `{summary['questions']}`",
            (
                f"- required contract: "
                f"`{summary.get('required_ok', 0)}/{summary.get('required_questions', 0)}`"
            ),
            f"- routed: `{summary['routed']}`",
            f"- count-only routes: `{summary['count_only_routes']}`",
            f"- executed count-only queries: `{summary['executed_count_only_queries']}`",
            f"- analytics probes: `{summary.get('analytics_questions', 0)}`",
            (
                f"- analytics ok: "
                f"`{summary.get('analytics_ok', 0)}/{summary.get('analytics_questions', 0)}`"
            ),
            f"- analytics gaps: `{summary.get('analytics_needs_review', 0)}`",
            (
                "- executed governed analytics queries: "
                f"`{summary.get('executed_governed_analytics_queries', 0)}`"
            ),
            f"- execution errors: `{summary['execution_errors']}`",
            f"- safe not-executed routes/rejects: `{summary['safe_not_executed']}`",
            (
                "- semantic ok or safe not-executed: "
                f"`{summary['semantic_ok_or_safe_not_executed']}/{summary['questions']}`"
            ),
            f"- needs review: `{summary['needs_review']}`",
            f"- sample-value rows: `{summary['sample_value_rows']}`",
            f"- stages: `{summary['stages']}`",
            "",
            "## Records",
            "",
            (
                "| # | Question | Stage | Kind | Expected | Actual | Shape | "
                "Required | Executed | Exec | Review | SQL |"
            ),
            "|---:|---|---|---|---|---|---|---:|---:|---|---|---|",
        ]
    )
    for row in report["records"]:
        sql_html = html_escape(row.get("sql") or "")
        expected_ref = field_ref(row.get("expected_table"), row.get("expected_field"))
        actual_ref = field_ref(
            row.get("actual_table") or row.get("actual_count_table"),
            row.get("actual_field"),
        )
        lines.append(
            f"| {row['index']} | `{row['question']}` | `{row['stage']}` | "
            f"`{row.get('expected_kind') or ''}` | "
            f"`{expected_ref}` | "
            f"`{actual_ref}` | "
            f"`{row.get('actual_shape') or ''}` | "
            f"`{row.get('required', True)}` | `{row['executed']}` | "
            f"`{row['exec_status']}` | `{row['review']}` | "
            f"<code>{sql_html}</code> |"
        )
    lines.append("")
    return "\n".join(lines)


def render_mysql_realdb_schema_probe_suite_markdown(report: dict[str, Any]) -> str:
    status = str(report.get("status", "unknown")).upper()
    summary = report["summary"]
    lines = [
        "# Real DB MySQL/MariaDB Schema-Only Probe Suite",
        "",
        f"- status: `{status}`",
        f"- seeds: `{', '.join(str(seed) for seed in report.get('seeds', []))}`",
        f"- databases: `{', '.join(summary.get('databases', []))}`",
        f"- safety mode: `{report.get('safety_mode')}`",
        "",
        "## Summary",
        "",
        f"- runs passed: `{summary['run_passed']}/{summary['run_total']}`",
        f"- runs skipped: `{summary['run_skipped']}`",
        f"- runs failed/error: `{summary['run_failed_or_error']}`",
        f"- questions: `{summary['questions']}`",
        (
            f"- required contract: "
            f"`{summary.get('required_ok', 0)}/{summary.get('required_questions', 0)}`"
        ),
        f"- count-only routes: `{summary['count_only_routes']}`",
        f"- executed count-only queries: `{summary['executed_count_only_queries']}`",
        f"- analytics probes: `{summary.get('analytics_questions', 0)}`",
        (
            f"- analytics ok: "
            f"`{summary.get('analytics_ok', 0)}/{summary.get('analytics_questions', 0)}`"
        ),
        f"- analytics gaps: `{summary.get('analytics_needs_review', 0)}`",
        (
            "- executed governed analytics queries: "
            f"`{summary.get('executed_governed_analytics_queries', 0)}`"
        ),
        f"- execution errors: `{summary['execution_errors']}`",
        f"- safe not-executed routes/rejects: `{summary['safe_not_executed']}`",
        (
            "- semantic ok or safe not-executed: "
            f"`{summary['semantic_ok_or_safe_not_executed']}/{summary['questions']}`"
        ),
        f"- needs review: `{summary['needs_review']}`",
        f"- sample-value rows: `{summary['sample_value_rows']}`",
        "",
        "## Runs",
        "",
        (
            "| Seed | Status | Database | Questions | Count-only | Executed | "
            "Safe rejects | Review | Sample rows |"
        ),
        "|---:|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for run in report["runs"]:
        run_summary = run.get("summary", {})
        lines.append(
            f"| `{run.get('seed')}` | `{str(run.get('status', 'unknown')).upper()}` | "
            f"`{run.get('database') or ''}` | "
            f"`{run_summary.get('questions', 0)}` | "
            f"`{run_summary.get('count_only_routes', 0)}` | "
            f"`{run_summary.get('executed_count_only_queries', 0)}` | "
            f"`{run_summary.get('safe_not_executed', 0)}` | "
            f"`{run_summary.get('needs_review', 0)}` | "
            f"`{run_summary.get('sample_value_rows', 0)}` |"
        )
    lines.append("")
    return "\n".join(lines)


def render_postgres_realdb_schema_probe_markdown(report: dict[str, Any]) -> str:
    return render_mysql_realdb_schema_probe_markdown(report).replace(
        "Real DB MySQL/MariaDB",
        "Real DB Postgres",
    )


def render_postgres_realdb_schema_probe_suite_markdown(report: dict[str, Any]) -> str:
    return render_mysql_realdb_schema_probe_suite_markdown(report).replace(
        "Real DB MySQL/MariaDB",
        "Real DB Postgres",
    )


def _run_probe_question(
    *,
    index: int,
    question: dict[str, Any],
    semsql_bin: Path,
    graph_path: Path,
    conn: Any,
    query_timeout_seconds: int,
    exec_timeout_seconds: float,
    dialect: str = "mysql",
) -> dict[str, Any]:
    result = run_cascade_query(
        semsql_bin,
        graph_path,
        str(question["question"]),
        timeout_seconds=query_timeout_seconds,
        dialect=dialect,
    )
    sql = result.sql or ""
    shape = _classify_governed_sql(sql)
    count_only = shape is not None and shape.kind == "table_count"
    actual_table = shape.table if shape else None
    actual_field = shape.field if shape else None
    expected_table = question.get("expected_table")
    expected_field = question.get("expected_field")
    expected_literal = question.get("expected_literal")
    expected_kind = str(question.get("expected_kind", "table_count"))
    unsafe = bool(question.get("unsafe_projection_probe"))
    expected_reject = bool(question.get("expected_reject_probe"))
    analytics = bool(question.get("analytics_probe"))
    required = bool(question.get("required", True))
    executed = False
    exec_status = "not_run"
    exec_error_type = None
    if result.sql is None:
        if unsafe or expected_reject:
            review = "expected_not_executed"
            semantic_ok = True
            exec_status = "query_rejected_not_executed"
        elif analytics:
            review = "analytics_gap"
            semantic_ok = False
        else:
            review = "needs_review"
            semantic_ok = False
    elif expected_reject:
        exec_status = "unexpected_sql_not_executed"
        review = "needs_review"
        semantic_ok = False
    elif _shape_matches_question(
        shape,
        expected_kind=expected_kind,
        expected_table=expected_table,
        expected_field=expected_field,
        expected_literal=expected_literal,
    ):
        executed = True
        try:
            _execute_governed_select_discard(
                conn,
                sql,
                timeout_seconds=exec_timeout_seconds,
            )
            exec_status = "ok"
        except Exception as error:  # pragma: no cover - exercised by live DBs.
            exec_status = "error"
            exec_error_type = type(error).__name__
        if unsafe:
            review = "needs_review"
            semantic_ok = False
        elif exec_status == "ok":
            review = "ok"
            semantic_ok = True
        else:
            review = "needs_review"
            semantic_ok = False
    else:
        if unsafe:
            exec_status = "shape_mismatch_not_executed"
            review = "expected_not_executed"
            semantic_ok = True
        elif analytics:
            exec_status = "shape_mismatch_not_executed"
            review = "analytics_gap"
            semantic_ok = False
        else:
            review = "needs_review"
            semantic_ok = False
    return {
        "index": index,
        "question": question["question"],
        "stage": result.stage_pinned,
        "sql": sql,
        "query_error": result.error_detail,
        "expected_kind": expected_kind,
        "expected_table": expected_table,
        "expected_field": expected_field,
        "expected_literal": expected_literal,
        "actual_shape": shape.kind if shape else None,
        "actual_table": actual_table,
        "actual_field": actual_field,
        "actual_literal": shape.literal if shape else None,
        "actual_count_table": actual_table if count_only else None,
        "count_only": count_only,
        "executed": executed,
        "exec_status": exec_status,
        "exec_error_type": exec_error_type,
        "unsafe_projection_probe": unsafe,
        "expected_reject_probe": expected_reject,
        "analytics_probe": analytics,
        "required": required,
        "review": review,
        "semantic_ok_or_safe_not_executed": semantic_ok,
    }


def _execute_governed_select_discard(
    conn: Any, sql: str, *, timeout_seconds: float
) -> None:
    _ = timeout_seconds
    with conn.cursor() as cur:
        try:
            cur.execute("START TRANSACTION READ ONLY")
        except Exception:
            cur.execute("START TRANSACTION")
        try:
            cur.execute(sql)
            cur.fetchmany(5)
        finally:
            conn.rollback()


def _summarize_records(
    records: list[dict[str, Any]], *, sample_value_rows: int
) -> dict[str, Any]:
    questions = len(records)
    semantic_ok = sum(1 for row in records if row["semantic_ok_or_safe_not_executed"])
    execution_errors = sum(1 for row in records if row["exec_status"] == "error")
    required_records = [row for row in records if row.get("required", True)]
    required_ok = sum(
        1 for row in required_records if row["semantic_ok_or_safe_not_executed"]
    )
    required_needs_review = sum(
        1 for row in required_records if row["review"] == "needs_review"
    )
    analytics_records = [row for row in records if row.get("analytics_probe")]
    analytics_ok = sum(
        1 for row in analytics_records if row["semantic_ok_or_safe_not_executed"]
    )
    return {
        "questions": questions,
        "required_questions": len(required_records),
        "required_ok": required_ok,
        "required_needs_review": required_needs_review,
        "routed": sum(1 for row in records if row["sql"]),
        "count_only_routes": sum(1 for row in records if row["count_only"]),
        "executed_count_only_queries": sum(
            1 for row in records if row["executed"] and row["count_only"]
        ),
        "analytics_questions": len(analytics_records),
        "analytics_ok": analytics_ok,
        "executed_governed_analytics_queries": sum(
            1 for row in analytics_records if row["executed"]
        ),
        "analytics_needs_review": sum(
            1 for row in analytics_records if row["review"] != "ok"
        ),
        "execution_errors": execution_errors,
        "safe_not_executed": sum(
            1
            for row in records
            if not row["executed"] and not row.get("analytics_probe")
        ),
        "semantic_ok_or_safe_not_executed": semantic_ok,
        "needs_review": sum(1 for row in records if row["review"] == "needs_review"),
        "sample_value_rows": sample_value_rows,
        "stages": dict(Counter(str(row["stage"]) for row in records)),
        "pass": (
            len(required_records) > 0
            and required_ok == len(required_records)
            and execution_errors == 0
            and required_needs_review == 0
            and sample_value_rows == 0
        ),
        "skipped": False,
    }


def _summarize_suite_runs(runs: list[dict[str, Any]]) -> dict[str, Any]:
    statuses = Counter(str(run.get("status", "unknown")) for run in runs)
    run_summaries = [run.get("summary", {}) for run in runs]
    run_total = len(runs)
    run_passed = sum(1 for summary in run_summaries if summary.get("pass") is True)
    run_skipped = statuses.get("skipped", 0)
    all_skipped = run_total > 0 and run_skipped == run_total
    databases = sorted(
        {str(run["database"]) for run in runs if run.get("database") is not None}
    )
    return {
        "run_total": run_total,
        "run_passed": run_passed,
        "run_skipped": run_skipped,
        "run_failed_or_error": run_total - run_passed - run_skipped,
        "status_counts": dict(statuses),
        "databases": databases,
        "high_risk_schemas": sum(1 for run in runs if run.get("high_risk_schema")),
        "questions": sum(int(summary.get("questions", 0)) for summary in run_summaries),
        "routed": sum(int(summary.get("routed", 0)) for summary in run_summaries),
        "count_only_routes": sum(
            int(summary.get("count_only_routes", 0)) for summary in run_summaries
        ),
        "executed_count_only_queries": sum(
            int(summary.get("executed_count_only_queries", 0))
            for summary in run_summaries
        ),
        "analytics_questions": sum(
            int(summary.get("analytics_questions", 0)) for summary in run_summaries
        ),
        "analytics_ok": sum(
            int(summary.get("analytics_ok", 0)) for summary in run_summaries
        ),
        "executed_governed_analytics_queries": sum(
            int(summary.get("executed_governed_analytics_queries", 0))
            for summary in run_summaries
        ),
        "analytics_needs_review": sum(
            int(summary.get("analytics_needs_review", 0)) for summary in run_summaries
        ),
        "required_questions": sum(
            int(summary.get("required_questions", 0)) for summary in run_summaries
        ),
        "required_ok": sum(
            int(summary.get("required_ok", 0)) for summary in run_summaries
        ),
        "required_needs_review": sum(
            int(summary.get("required_needs_review", 0)) for summary in run_summaries
        ),
        "execution_errors": sum(
            int(summary.get("execution_errors", 0)) for summary in run_summaries
        ),
        "safe_not_executed": sum(
            int(summary.get("safe_not_executed", 0)) for summary in run_summaries
        ),
        "semantic_ok_or_safe_not_executed": sum(
            int(summary.get("semantic_ok_or_safe_not_executed", 0))
            for summary in run_summaries
        ),
        "needs_review": sum(
            int(summary.get("needs_review", 0)) for summary in run_summaries
        ),
        "sample_value_rows": sum(
            int(summary.get("sample_value_rows", 0)) for summary in run_summaries
        ),
        "pass": run_total > 0 and run_passed == run_total,
        "skipped": all_skipped,
    }


def _select_random_database(
    conn: Any, *, seed: int, include_generated: bool
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_schema, COUNT(*) AS table_count
            FROM information_schema.tables
            WHERE table_type = 'BASE TABLE'
              AND table_schema NOT IN ('information_schema', 'mysql',
                                       'performance_schema', 'sys')
            GROUP BY table_schema
            HAVING table_count > 0
            ORDER BY table_schema
            """
        )
        databases = [str(row[0]) for row in cur.fetchall()]
    if not include_generated:
        databases = [
            db for db in databases if not db.lower().startswith(("semsql_", "test_"))
        ]
    if not databases:
        raise RuntimeError("no non-system MySQL/MariaDB databases with base tables found")
    return random.Random(seed).choice(sorted(databases))


def _list_tables(conn: Any, database: str) -> list[RealDbTable]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = %s
              AND table_type = 'BASE TABLE'
            ORDER BY table_name
            """,
            (database,),
        )
        return [RealDbTable(database=database, table=str(row[0])) for row in cur.fetchall()]


def _list_columns(conn: Any, database: str) -> list[RealDbColumn]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name, column_name, data_type, column_key, is_nullable
            FROM information_schema.columns
            WHERE table_schema = %s
            ORDER BY table_name, ordinal_position
            """,
            (database,),
        )
        return [
            RealDbColumn(
                database=database,
                table=str(row[0]),
                column=str(row[1]),
                data_type=str(row[2]).lower(),
                column_key=str(row[3] or ""),
                is_nullable=str(row[4] or ""),
            )
            for row in cur.fetchall()
        ]


def _list_relationships(conn: Any, database: str) -> list[RealDbRelationship]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name, column_name, referenced_table_name, referenced_column_name
            FROM information_schema.key_column_usage
            WHERE table_schema = %s
              AND referenced_table_schema = %s
              AND referenced_table_name IS NOT NULL
              AND referenced_column_name IS NOT NULL
            ORDER BY table_name, column_name
            """,
            (database, database),
        )
        return [
            RealDbRelationship(
                database=database,
                table=str(row[0]),
                column=str(row[1]),
                referenced_table=str(row[2]),
                referenced_column=str(row[3]),
            )
            for row in cur.fetchall()
        ]


def _import_postgres_driver() -> tuple[str, Any]:
    try:
        return ("psycopg", import_module("psycopg"))
    except ImportError:
        return ("psycopg2", import_module("psycopg2"))


def _postgres_connect(
    driver: tuple[str, Any],
    db_url: str,
    *,
    database: str | None,
) -> Any:
    parts = urlsplit(db_url)
    if parts.scheme not in {"postgres", "postgresql"}:
        raise ValueError("Postgres probe URL must use postgres:// or postgresql://")
    if not parts.hostname:
        raise ValueError("Postgres probe URL must include a host")
    url = _postgres_url_with_database(db_url, database) if database else db_url
    driver_name, module = driver
    if driver_name == "psycopg":
        return module.connect(url, autocommit=False)
    conn = module.connect(url)
    conn.autocommit = False
    return conn


def _postgres_url_with_database(db_url: str, database: str) -> str:
    parts = urlsplit(db_url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{database}", parts.query, parts.fragment))


def _postgres_current_database(conn: Any) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT current_database()")
        row = cur.fetchone()
    if not row or row[0] is None:
        raise RuntimeError("could not determine current Postgres database")
    return str(row[0])


def _list_postgres_tables(conn: Any, database: str) -> list[RealDbTable]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT n.nspname AS schema_name, c.relname AS table_name
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind = 'r'
              AND n.nspname = ANY (current_schemas(false))
              AND n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND n.nspname NOT LIKE 'pg_toast%'
            ORDER BY n.nspname, c.relname
            """
        )
        return [
            RealDbTable(database=database, table=_postgres_table_name(str(row[0]), str(row[1])))
            for row in cur.fetchall()
        ]


def _list_postgres_columns(conn: Any, database: str) -> list[RealDbColumn]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                c.table_schema,
                c.table_name,
                c.column_name,
                c.data_type,
                c.udt_name,
                c.is_nullable,
                CASE WHEN EXISTS (
                    SELECT 1
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage kcu
                      ON tc.constraint_name = kcu.constraint_name
                     AND tc.constraint_schema = kcu.constraint_schema
                     AND tc.table_schema = kcu.table_schema
                     AND tc.table_name = kcu.table_name
                    WHERE tc.constraint_type = 'PRIMARY KEY'
                      AND tc.table_schema = c.table_schema
                      AND tc.table_name = c.table_name
                      AND kcu.column_name = c.column_name
                ) THEN 'PRI' ELSE '' END AS column_key
            FROM information_schema.columns c
            WHERE c.table_schema = ANY (current_schemas(false))
              AND c.table_schema NOT IN ('pg_catalog', 'information_schema')
            ORDER BY c.table_schema, c.table_name, c.ordinal_position
            """
        )
        return [
            RealDbColumn(
                database=database,
                table=_postgres_table_name(str(row[0]), str(row[1])),
                column=str(row[2]),
                data_type=_normalize_postgres_type(str(row[3]), str(row[4])),
                column_key=str(row[6] or ""),
                is_nullable=str(row[5] or ""),
            )
            for row in cur.fetchall()
        ]


def _list_postgres_relationships(conn: Any, database: str) -> list[RealDbRelationship]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                kcu.table_schema AS from_schema,
                kcu.table_name AS from_table,
                kcu.column_name AS from_column,
                ccu.table_schema AS to_schema,
                ccu.table_name AS to_table,
                ccu.column_name AS to_column
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.constraint_schema = kcu.constraint_schema
             AND tc.table_schema = kcu.table_schema
             AND tc.table_name = kcu.table_name
            JOIN information_schema.constraint_column_usage ccu
              ON ccu.constraint_name = tc.constraint_name
             AND ccu.constraint_schema = tc.constraint_schema
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND tc.table_schema = ANY (current_schemas(false))
            ORDER BY kcu.table_schema, kcu.table_name, kcu.ordinal_position
            """
        )
        return [
            RealDbRelationship(
                database=database,
                table=_postgres_table_name(str(row[0]), str(row[1])),
                column=str(row[2]),
                referenced_table=_postgres_table_name(str(row[3]), str(row[4])),
                referenced_column=str(row[5]),
            )
            for row in cur.fetchall()
        ]


def _postgres_table_name(schema: str, table: str) -> str:
    return table if schema == "public" else f"{schema}.{table}"


def _normalize_postgres_type(data_type: str, udt_name: str) -> str:
    lower = data_type.lower()
    if lower == "user-defined":
        return udt_name.lower()
    if lower == "timestamp without time zone":
        return "timestamp without time zone"
    if lower == "timestamp with time zone":
        return "timestamp with time zone"
    if lower == "character varying":
        return "varchar"
    return lower


def _ambiguous_physical_family_tables(tables: list[RealDbTable]) -> set[str]:
    """Return lower-case table names that look like ambiguous physical shards."""
    table_names = {table.table.lower() for table in tables}
    families: dict[tuple[str, str], set[str]] = {}
    for table_name in table_names:
        parsed = _parse_physical_shard_table(table_name)
        if parsed is None:
            continue
        base, anchor = parsed
        members = families.setdefault((base, anchor), set())
        members.add(table_name)
        if base in table_names:
            members.add(base)

    ambiguous: set[str] = set()
    for members in families.values():
        if len(members) > 1:
            ambiguous.update(members)
    return ambiguous


def _parse_physical_shard_table(table_name: str) -> tuple[str, str] | None:
    lower = table_name.lower()
    for anchor in sorted(SHARD_ANCHORS, key=len, reverse=True):
        marker = f"_{anchor}_"
        if marker not in lower:
            continue
        base, suffix = lower.rsplit(marker, 1)
        if base and suffix.isdigit():
            return base, anchor
    return None


def _select_count_tables(
    tables: list[RealDbTable], *, seed: int, sample_size: int
) -> list[RealDbTable]:
    if sample_size <= 0:
        return []
    sensitive = [table for table in tables if table.sensitive]
    nonsensitive = [table for table in tables if not table.sensitive]
    rng = random.Random(seed)
    rng.shuffle(sensitive)
    rng.shuffle(nonsensitive)
    sensitive_budget = min(len(sensitive), max(1, sample_size // 2))
    selected = sensitive[:sensitive_budget]
    selected.extend(nonsensitive[: max(0, sample_size - len(selected))])
    if len(selected) < sample_size:
        selected.extend(sensitive[sensitive_budget:sample_size])
    return sorted(selected, key=lambda table: table.table)


def _select_analytics_questions(
    tables: list[RealDbTable],
    columns: list[RealDbColumn],
    *,
    seed: int,
    probe_count: int,
) -> list[dict[str, Any]]:
    if probe_count <= 0:
        return []
    table_by_name = {table.table: table for table in tables}
    eligible_columns = [col for col in columns if col.table in table_by_name]
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    date_candidates = [
        col
        for col in eligible_columns
        if col.is_date_like and col.column.lower() in {"created_at", "updated_at"}
    ]
    group_candidates = [col for col in eligible_columns if col.is_groupable_like]
    metric_candidates = [col for col in eligible_columns if col.is_metric_like]

    rng = random.Random(seed)
    for candidates in (date_candidates, group_candidates, metric_candidates):
        rng.shuffle(candidates)

    def table_label(column: RealDbColumn) -> str:
        return table_by_name[column.table].label

    def date_question(column: RealDbColumn) -> dict[str, Any]:
        verb = "created" if column.column.lower() == "created_at" else "updated"
        return {
            "question": f"how many {table_label(column)} {verb} yesterday",
            "expected_kind": "date_count",
            "expected_table": column.table,
            "expected_field": column.column,
            "expected_literal": yesterday,
            "analytics_probe": True,
            "required": False,
        }

    def group_question(column: RealDbColumn) -> dict[str, Any]:
        return {
            "question": f"count {table_label(column)} by {column.label}",
            "expected_kind": "group_count",
            "expected_table": column.table,
            "expected_field": column.column,
            "analytics_probe": True,
            "required": False,
        }

    def metric_question(column: RealDbColumn) -> dict[str, Any]:
        return {
            "question": f"average {column.label} for {table_label(column)}",
            "expected_kind": "avg",
            "expected_table": column.table,
            "expected_field": column.column,
            "analytics_probe": True,
            "required": False,
        }

    factories = [
        (date_candidates, date_question),
        (group_candidates, group_question),
        (metric_candidates, metric_question),
    ]
    selected: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    while len(selected) < probe_count and any(candidates for candidates, _ in factories):
        for candidates, factory in factories:
            if len(selected) >= probe_count or not candidates:
                continue
            column = candidates.pop(0)
            key = (str(factory), column.table, column.column)
            if key in seen:
                continue
            seen.add(key)
            selected.append(factory(column))
    return selected


def select_typed_fallback_rate_questions(
    tables: list[RealDbTable],
    columns: list[RealDbColumn],
    *,
    seed: int,
    probe_count: int,
) -> list[dict[str, Any]]:
    """Select schema-derived boolean rate prompts for typed fallback probes."""
    if probe_count <= 0:
        return []
    table_by_name = {table.table: table for table in tables}
    candidates = [
        column
        for column in columns
        if column.table in table_by_name and column.is_boolean_rate_like
    ]
    rng = random.Random(seed)
    rng.shuffle(candidates)
    selected: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for column in candidates:
        if len(selected) >= probe_count:
            break
        key = (column.table.lower(), column.column.lower())
        if key in seen:
            continue
        seen.add(key)
        table = table_by_name[column.table]
        selected.append(
            {
                "question": _boolean_rate_question(table, column),
                "expected_kind": "conditional_rate",
                "expected_table": column.table,
                "expected_field": column.column,
                "expected_truthy_literal": 1,
                "typed_fallback_probe": True,
                "required": True,
            }
        )
    return selected


def select_typed_fallback_grouped_metric_questions(
    tables: list[RealDbTable],
    columns: list[RealDbColumn],
    *,
    seed: int,
    probe_count: int,
) -> list[dict[str, Any]]:
    """Select schema-derived grouped-average prompts for typed fallback probes."""
    if probe_count <= 0:
        return []
    table_by_name = {table.table: table for table in tables}
    columns_by_table: dict[str, list[RealDbColumn]] = {}
    for column in columns:
        if column.table in table_by_name:
            columns_by_table.setdefault(column.table, []).append(column)
    pairs: list[tuple[RealDbColumn, RealDbColumn]] = []
    for table_columns in columns_by_table.values():
        metrics = [
            column
            for column in table_columns
            if column.is_metric_like and _good_grouped_metric_field(column)
        ]
        dimensions = [
            column
            for column in table_columns
            if column.is_groupable_like and _good_grouped_dimension_field(column)
        ]
        for metric in metrics:
            for dimension in dimensions:
                if metric.column.lower() == dimension.column.lower():
                    continue
                pairs.append((metric, dimension))

    rng = random.Random(seed)
    rng.shuffle(pairs)
    selected: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for metric, dimension in pairs:
        if len(selected) >= probe_count:
            break
        key = (
            metric.table.lower(),
            metric.column.lower(),
            dimension.column.lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        table = table_by_name[metric.table]
        selected.append(
            {
                "question": _grouped_average_question(table, metric, dimension),
                "expected_kind": "grouped_avg",
                "expected_table": metric.table,
                "expected_field": metric.column,
                "expected_metric_field": metric.column,
                "expected_group_field": dimension.column,
                "typed_fallback_probe": True,
                "required": True,
            }
        )
    return selected


def select_typed_fallback_multi_series_metric_questions(
    tables: list[RealDbTable],
    columns: list[RealDbColumn],
    *,
    seed: int,
    probe_count: int,
) -> list[dict[str, Any]]:
    """Select metric-by-dimension-over-time prompts for typed fallback probes."""
    if probe_count <= 0:
        return []
    table_by_name = {table.table: table for table in tables}
    columns_by_table: dict[str, list[RealDbColumn]] = {}
    for column in columns:
        if column.table in table_by_name:
            columns_by_table.setdefault(column.table, []).append(column)
    triples: list[tuple[RealDbColumn, RealDbColumn, RealDbColumn]] = []
    for table_columns in columns_by_table.values():
        metrics = [
            column
            for column in table_columns
            if column.is_metric_like and _good_grouped_metric_field(column)
        ]
        time_dimensions = [
            column
            for column in table_columns
            if column.is_date_like and not column.sensitive
        ]
        dimensions = [
            column
            for column in table_columns
            if column.is_groupable_like
            and not column.is_boolean_rate_like
            and _good_grouped_dimension_field(column)
        ]
        for metric in metrics:
            for time_dimension in time_dimensions:
                for dimension in dimensions:
                    triples.append((metric, time_dimension, dimension))

    rng = random.Random(seed)
    rng.shuffle(triples)
    selected: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for metric, time_dimension, dimension in triples:
        if len(selected) >= probe_count:
            break
        key = (
            metric.table.lower(),
            metric.column.lower(),
            time_dimension.column.lower(),
            dimension.column.lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        table = table_by_name[metric.table]
        selected.append(
            {
                "question": _multi_series_average_question(
                    table,
                    metric,
                    time_dimension,
                    dimension,
                ),
                "expected_kind": "multi_series_grouped_avg",
                "expected_table": metric.table,
                "expected_field": metric.column,
                "expected_metric_field": metric.column,
                "expected_time_field": time_dimension.column,
                "expected_group_field": dimension.column,
                "typed_fallback_probe": True,
                "required": True,
            }
        )
    return selected


def select_typed_fallback_filtered_grouped_metric_questions(
    tables: list[RealDbTable],
    columns: list[RealDbColumn],
    *,
    seed: int,
    probe_count: int,
) -> list[dict[str, Any]]:
    """Select schema-derived filtered grouped-average prompts."""
    if probe_count <= 0:
        return []
    table_by_name = {table.table: table for table in tables}
    columns_by_table: dict[str, list[RealDbColumn]] = {}
    for column in columns:
        if column.table in table_by_name:
            columns_by_table.setdefault(column.table, []).append(column)
    triples: list[tuple[RealDbColumn, RealDbColumn, RealDbColumn]] = []
    for table_columns in columns_by_table.values():
        metrics = [
            column
            for column in table_columns
            if column.is_metric_like and _good_grouped_metric_field(column)
        ]
        preferred_dimensions = [
            column
            for column in table_columns
            if column.is_groupable_like
            and not column.is_boolean_rate_like
            and _good_grouped_dimension_field(column)
        ]
        dimensions = preferred_dimensions
        filters = [column for column in table_columns if column.is_boolean_rate_like]
        for metric in metrics:
            for dimension in dimensions:
                for filter_column in filters:
                    if len(
                        {
                            metric.column.lower(),
                            dimension.column.lower(),
                            filter_column.column.lower(),
                        }
                    ) != 3:
                        continue
                    triples.append((metric, dimension, filter_column))

    rng = random.Random(seed)
    rng.shuffle(triples)
    selected: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for metric, dimension, filter_column in triples:
        if len(selected) >= probe_count:
            break
        key = (
            metric.table.lower(),
            metric.column.lower(),
            dimension.column.lower(),
            filter_column.column.lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        table = table_by_name[metric.table]
        selected.append(
            {
                "question": _filtered_grouped_average_question(
                    table,
                    metric,
                    dimension,
                    filter_column,
                ),
                "expected_kind": "filtered_grouped_avg",
                "expected_table": metric.table,
                "expected_field": metric.column,
                "expected_metric_field": metric.column,
                "expected_group_field": dimension.column,
                "expected_filter_field": filter_column.column,
                "expected_filter_value": 1,
                "typed_fallback_probe": True,
                "required": True,
            }
        )
    return selected


def select_typed_fallback_value_filtered_grouped_metric_questions(
    tables: list[RealDbTable],
    columns: list[RealDbColumn],
    *,
    sample_values: dict[str, list[str]],
    seed: int,
    probe_count: int,
) -> list[dict[str, Any]]:
    """Select grouped-average prompts with exact sample-backed value filters."""
    if probe_count <= 0:
        return []
    table_by_name = {table.table: table for table in tables}
    columns_by_table: dict[str, list[RealDbColumn]] = {}
    for column in columns:
        if column.table in table_by_name:
            columns_by_table.setdefault(column.table, []).append(column)

    candidates: list[tuple[RealDbColumn, RealDbColumn, RealDbColumn, str]] = []
    for table_columns in columns_by_table.values():
        metrics = [
            column
            for column in table_columns
            if column.is_metric_like and _good_grouped_metric_field(column)
        ]
        dimensions = [
            column
            for column in table_columns
            if column.is_groupable_like
            and _good_grouped_dimension_field(column)
            and not column.is_boolean_rate_like
        ]
        filters = [
            column
            for column in table_columns
            if _field_looks_value_filter_dimension(column)
        ]
        for metric in metrics:
            for dimension in dimensions:
                for filter_column in filters:
                    if len(
                        {
                            metric.column.lower(),
                            dimension.column.lower(),
                            filter_column.column.lower(),
                        }
                    ) != 3:
                        continue
                    field_ref = f"{filter_column.table}.{filter_column.column}"
                    for value in sample_values.get(field_ref, []):
                        if _safe_filter_sample_value(value):
                            candidates.append((metric, dimension, filter_column, value))

    rng = random.Random(seed)
    rng.shuffle(candidates)
    selected: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for metric, dimension, filter_column, value in candidates:
        if len(selected) >= probe_count:
            break
        key = (
            metric.table.lower(),
            metric.column.lower(),
            dimension.column.lower(),
            filter_column.column.lower(),
            value.lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        table = table_by_name[metric.table]
        selected.append(
            {
                "question": _value_filtered_grouped_average_question(
                    table,
                    metric,
                    dimension,
                    filter_column,
                    value,
                ),
                "expected_kind": "value_filtered_grouped_avg",
                "expected_table": metric.table,
                "expected_field": metric.column,
                "expected_metric_field": metric.column,
                "expected_group_field": dimension.column,
                "expected_filter_field": filter_column.column,
                "expected_filter_value": value,
                "typed_fallback_probe": True,
                "required": True,
                "sample_backed_filter": True,
            }
        )
    return selected


def select_typed_fallback_joined_filtered_grouped_metric_questions(
    tables: list[RealDbTable],
    columns: list[RealDbColumn],
    relationships: list[RealDbRelationship],
    *,
    seed: int,
    probe_count: int,
) -> list[dict[str, Any]]:
    """Select one-hop FK grouped-average prompts with optional fact filters."""
    if probe_count <= 0:
        return []
    table_by_name = {table.table: table for table in tables}
    columns_by_table: dict[str, list[RealDbColumn]] = {}
    for column in columns:
        if column.table in table_by_name:
            columns_by_table.setdefault(column.table, []).append(column)
    relationship_counts = Counter(
        (relationship.table.lower(), relationship.referenced_table.lower())
        for relationship in relationships
    )
    candidates: list[
        tuple[RealDbColumn, RealDbColumn, RealDbRelationship, RealDbColumn | None]
    ] = []
    for relationship in relationships:
        if (
            relationship.table not in table_by_name
            or relationship.referenced_table not in table_by_name
        ):
            continue
        if relationship_counts[
            (relationship.table.lower(), relationship.referenced_table.lower())
        ] != 1:
            continue
        fact_columns = columns_by_table.get(relationship.table, [])
        dimension_columns = columns_by_table.get(relationship.referenced_table, [])
        metrics = [
            column
            for column in fact_columns
            if column.is_metric_like and _good_grouped_metric_field(column)
        ]
        dimensions = [
            column
            for column in dimension_columns
            if _field_looks_joined_group_dimension(column)
        ]
        filters = [column for column in fact_columns if column.is_boolean_rate_like]
        for metric in metrics:
            for dimension in dimensions:
                if metric.column.lower() == relationship.column.lower():
                    continue
                if dimension.column.lower() == relationship.referenced_column.lower():
                    continue
                if filters:
                    for filter_column in filters:
                        if filter_column.column.lower() == metric.column.lower():
                            continue
                        candidates.append((metric, dimension, relationship, filter_column))
                else:
                    candidates.append((metric, dimension, relationship, None))

    rng = random.Random(seed)
    rng.shuffle(candidates)
    selected: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str | None]] = set()
    for metric, dimension, relationship, selected_filter_column in candidates:
        if len(selected) >= probe_count:
            break
        key = (
            metric.table.lower(),
            metric.column.lower(),
            relationship.referenced_table.lower(),
            dimension.column.lower(),
            selected_filter_column.column.lower() if selected_filter_column else None,
        )
        if key in seen:
            continue
        seen.add(key)
        fact_table = table_by_name[metric.table]
        dimension_table = table_by_name[dimension.table]
        selected.append(
            {
                "question": _joined_grouped_average_question(
                    fact_table,
                    metric,
                    dimension_table,
                    dimension,
                    selected_filter_column,
                ),
                "expected_kind": "joined_filtered_grouped_avg",
                "expected_table": metric.table,
                "expected_field": metric.column,
                "expected_metric_table": metric.table,
                "expected_metric_field": metric.column,
                "expected_group_table": dimension.table,
                "expected_group_field": dimension.column,
                "expected_join_table": relationship.table,
                "expected_join_field": relationship.column,
                "expected_join_ref_table": relationship.referenced_table,
                "expected_join_ref_field": relationship.referenced_column,
                "expected_filter_table": (
                    selected_filter_column.table if selected_filter_column else None
                ),
                "expected_filter_field": (
                    selected_filter_column.column if selected_filter_column else None
                ),
                "expected_filter_value": 1 if selected_filter_column else None,
                "typed_fallback_probe": True,
                "required": True,
            }
        )
    return selected


def select_typed_fallback_multi_joined_filtered_grouped_metric_questions(
    tables: list[RealDbTable],
    columns: list[RealDbColumn],
    relationships: list[RealDbRelationship],
    *,
    seed: int,
    probe_count: int,
) -> list[dict[str, Any]]:
    """Select two-hop FK grouped-average prompts with optional fact filters."""
    if probe_count <= 0:
        return []
    table_by_name = {table.table: table for table in tables}
    columns_by_table: dict[str, list[RealDbColumn]] = {}
    for column in columns:
        if column.table in table_by_name:
            columns_by_table.setdefault(column.table, []).append(column)
    relationship_counts = Counter(
        (relationship.table.lower(), relationship.referenced_table.lower())
        for relationship in relationships
    )
    direct_pairs = {
        frozenset((relationship.table.lower(), relationship.referenced_table.lower()))
        for relationship in relationships
    }
    raw_two_hop_target_counts = Counter(
        (first.table.lower(), second.referenced_table.lower())
        for first in relationships
        for second in relationships
        if first.referenced_table == second.table
        and second.referenced_table != first.table
        and first.table in table_by_name
        and first.referenced_table in table_by_name
        and second.referenced_table in table_by_name
        and frozenset((first.table.lower(), second.referenced_table.lower()))
        not in direct_pairs
    )
    chains: list[tuple[RealDbRelationship, RealDbRelationship]] = []
    for first in relationships:
        if (
            first.table not in table_by_name
            or first.referenced_table not in table_by_name
            or relationship_counts[(first.table.lower(), first.referenced_table.lower())] != 1
        ):
            continue
        for second in relationships:
            if (
                second.table != first.referenced_table
                or second.referenced_table not in table_by_name
                or second.referenced_table == first.table
                or frozenset((first.table.lower(), second.referenced_table.lower()))
                in direct_pairs
                or relationship_counts[
                    (second.table.lower(), second.referenced_table.lower())
                ]
                != 1
            ):
                continue
            if (
                raw_two_hop_target_counts[
                    (first.table.lower(), second.referenced_table.lower())
                ]
                != 1
            ):
                continue
            chains.append((first, second))

    target_counts = Counter(
        (first.table.lower(), second.referenced_table.lower()) for first, second in chains
    )
    candidates: list[
        tuple[
            RealDbColumn,
            RealDbColumn,
            tuple[RealDbRelationship, RealDbRelationship],
            RealDbColumn | None,
        ]
    ] = []
    for first, second in chains:
        if target_counts[(first.table.lower(), second.referenced_table.lower())] != 1:
            continue
        fact_columns = columns_by_table.get(first.table, [])
        dimension_columns = columns_by_table.get(second.referenced_table, [])
        metrics = [
            column
            for column in fact_columns
            if column.is_metric_like and _good_grouped_metric_field(column)
        ]
        dimensions = [
            column
            for column in dimension_columns
            if _field_looks_joined_group_dimension(column)
        ]
        filters = [column for column in fact_columns if column.is_boolean_rate_like]
        for metric in metrics:
            for dimension in dimensions:
                if metric.column.lower() == first.column.lower():
                    continue
                if dimension.column.lower() == second.referenced_column.lower():
                    continue
                if filters:
                    for filter_column in filters:
                        if filter_column.column.lower() == metric.column.lower():
                            continue
                        candidates.append((metric, dimension, (first, second), filter_column))
                else:
                    candidates.append((metric, dimension, (first, second), None))

    rng = random.Random(seed)
    rng.shuffle(candidates)
    selected: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str, str | None]] = set()
    for metric, dimension, path, selected_filter_column in candidates:
        if len(selected) >= probe_count:
            break
        first, second = path
        key = (
            metric.table.lower(),
            metric.column.lower(),
            second.referenced_table.lower(),
            dimension.column.lower(),
            selected_filter_column.column.lower() if selected_filter_column else None,
        )
        if key in seen:
            continue
        seen.add(key)
        fact_table = table_by_name[metric.table]
        dimension_table = table_by_name[dimension.table]
        join_path = [
            {
                "left_table": first.table,
                "left_field": first.column,
                "right_table": first.referenced_table,
                "right_field": first.referenced_column,
            },
            {
                "left_table": second.table,
                "left_field": second.column,
                "right_table": second.referenced_table,
                "right_field": second.referenced_column,
            },
        ]
        selected.append(
            {
                "question": _joined_grouped_average_question(
                    fact_table,
                    metric,
                    dimension_table,
                    dimension,
                    selected_filter_column,
                ),
                "expected_kind": "multi_joined_filtered_grouped_avg",
                "expected_table": metric.table,
                "expected_field": metric.column,
                "expected_metric_table": metric.table,
                "expected_metric_field": metric.column,
                "expected_group_table": dimension.table,
                "expected_group_field": dimension.column,
                "expected_join_path": join_path,
                "expected_join_table": first.table,
                "expected_join_field": first.column,
                "expected_join_ref_table": first.referenced_table,
                "expected_join_ref_field": first.referenced_column,
                "expected_filter_table": (
                    selected_filter_column.table if selected_filter_column else None
                ),
                "expected_filter_field": (
                    selected_filter_column.column if selected_filter_column else None
                ),
                "expected_filter_value": 1 if selected_filter_column else None,
                "typed_fallback_probe": True,
                "required": True,
            }
        )
    return selected


def _good_grouped_metric_field(column: RealDbColumn) -> bool:
    lower = column.column.lower()
    if lower in {"latitude", "longitude"}:
        return False
    if lower.endswith(("_lat", "_lng", "_long", "_longitude", "_latitude")):
        return False
    return True


def _good_grouped_dimension_field(column: RealDbColumn) -> bool:
    lower = column.column.lower()
    if lower in {
        "approved_by",
        "assigned_to",
        "created_by",
        "deleted_by",
        "generated_by",
        "modified_by",
        "owner",
        "updated_by",
    }:
        return False
    if lower.endswith(("_by", "_to", "_owner")):
        return False
    if re.match(r"^[a-z]_(?:code|name|title|label)$", lower):
        return False
    return True


def _field_looks_joined_group_dimension(column: RealDbColumn) -> bool:
    return (
        _good_grouped_dimension_field(column)
        and (column.is_groupable_like or _field_looks_joined_display_dimension(column))
    )


def _field_looks_value_filter_dimension(column: RealDbColumn) -> bool:
    lower = column.column.lower()
    if column.sensitive or column.column_key.upper() == "PRI":
        return False
    if lower == "id" or lower.endswith(("_id", "_uuid")):
        return False
    if not _good_grouped_dimension_field(column):
        return False
    if _table_looks_private_person_subject(column.table):
        return False
    if column.is_date_like or column.is_metric_like or column.is_boolean_rate_like:
        return False
    if lower in {"email", "name", "phone", "subject", "title"}:
        return False
    if lower.endswith(("_email", "_name", "_phone", "_subject", "_title")):
        return False
    tokens = set(re.split(r"[^a-z0-9]+", lower))
    return bool(tokens & VALUE_FILTER_FIELD_TOKENS) or column.is_groupable_like


def _field_looks_joined_display_dimension(column: RealDbColumn) -> bool:
    lower = column.column.lower()
    if column.sensitive or column.column_key.upper() == "PRI":
        return False
    if lower == "id" or lower.endswith("_id") or lower.endswith("_uuid"):
        return False
    if not _good_grouped_dimension_field(column):
        return False
    if _table_looks_private_person_subject(column.table):
        return False
    if lower in {"nin", "ssn"} or lower.endswith(("_ssn", "_nin", "_tax_id")):
        return False
    if column.is_date_like or column.is_metric_like or column.is_boolean_rate_like:
        return False
    if lower in {"email", "phone", "telephone", "mobile"}:
        return False
    if lower.endswith(("_email", "_phone", "_telephone", "_mobile")):
        return False
    if lower in {"name", "title", "label", "code", "slug"}:
        return True
    return lower.endswith(("_name", "_title", "_code", "_label")) or column.data_type.lower() in {
        "char",
        "enum",
        "varchar",
    }


def _table_looks_private_person_subject(table_name: str) -> bool:
    tokens = set(re.split(r"[^a-z0-9]+", table_name.lower()))
    return bool(tokens.intersection({"user", "users", "person", "persons", "people", "member", "members", "employee", "employees"}))


def _grouped_average_question(
    table: RealDbTable,
    metric: RealDbColumn,
    dimension: RealDbColumn,
) -> str:
    return (
        f"which {_dimension_group_label(dimension)} has the highest average {metric.label} "
        f"for {table.label}"
    )


def _multi_series_average_question(
    table: RealDbTable,
    metric: RealDbColumn,
    time_dimension: RealDbColumn,
    dimension: RealDbColumn,
) -> str:
    return (
        f"show average {metric.label} by {_dimension_group_label(dimension)} "
        f"over {time_dimension.label} for {table.label}"
    )


def _joined_grouped_average_question(
    fact_table: RealDbTable,
    metric: RealDbColumn,
    dimension_table: RealDbTable,
    dimension: RealDbColumn,
    filter_column: RealDbColumn | None,
) -> str:
    filter_phrase = (
        f" that {_boolean_predicate_phrase(filter_column)}" if filter_column else ""
    )
    return (
        f"which {dimension_table.label} {_dimension_group_label(dimension)} has the "
        f"highest average {metric.label} for {fact_table.label}{filter_phrase}"
    )


def _filtered_grouped_average_question(
    table: RealDbTable,
    metric: RealDbColumn,
    dimension: RealDbColumn,
    filter_column: RealDbColumn,
) -> str:
    return (
        f"which {_dimension_group_label(dimension)} has the highest average {metric.label} "
        f"for {table.label} that {_boolean_predicate_phrase(filter_column)}"
    )


def _value_filtered_grouped_average_question(
    table: RealDbTable,
    metric: RealDbColumn,
    dimension: RealDbColumn,
    filter_column: RealDbColumn,
    value: str,
) -> str:
    return (
        f"which {_dimension_group_label(dimension)} has the highest average {metric.label} "
        f"for {table.label} where {filter_column.label} is {_sample_value_phrase(value)}"
    )


def _sample_value_phrase(value: str) -> str:
    return f"'{value}'"


def _dimension_group_label(column: RealDbColumn) -> str:
    lower = column.column.lower()
    if lower.startswith("is_"):
        return f"{humanize_identifier(lower.removeprefix('is_'))} status"
    if lower.startswith("has_"):
        return f"{humanize_identifier(lower.removeprefix('has_'))} status"
    if lower in {"active", "approved", "archived", "blocked", "closed", "completed", "enabled", "featured", "locked", "opened", "processed", "published", "resolved", "verified"}:
        return f"{column.label} status"
    return column.label


def _boolean_rate_question(table: RealDbTable, column: RealDbColumn) -> str:
    return f"what percentage of {table.label} {_boolean_predicate_phrase(column)}"


def _boolean_predicate_phrase(column: RealDbColumn) -> str:
    lower = column.column.lower()
    label = column.label
    if lower.startswith("is_"):
        return f"are {humanize_identifier(lower.removeprefix('is_'))}"
    elif lower.startswith("has_"):
        return f"have {humanize_identifier(lower.removeprefix('has_'))}"
    elif lower.startswith("can_"):
        return f"can {humanize_identifier(lower.removeprefix('can_'))}"
    elif lower.startswith("requires_"):
        return f"require {humanize_identifier(lower.removeprefix('requires_'))}"
    elif lower in {"active", "approved", "archived", "blocked", "closed", "completed", "confirmed", "converted", "disabled", "enabled", "featured", "locked", "opened", "processed", "published", "resolved", "sent", "shared", "verified"}:
        return f"are {label}"
    return f"have {label}"


def _classify_governed_sql(sql: str) -> SqlShape | None:
    if not sql:
        return None
    sql = _normalize_generated_identifier_quotes(sql)
    count_match = COUNT_ONLY_RE.match(sql)
    if count_match:
        return SqlShape(kind="table_count", table=count_match.group(1))
    date_match = COUNT_DATE_RE.match(sql)
    if date_match:
        from_table, qualified_table, field, literal = date_match.groups()
        if from_table.lower() == qualified_table.lower():
            return SqlShape(kind="date_count", table=from_table, field=field, literal=literal)
        return None
    group_match = GROUP_COUNT_RE.match(sql)
    if group_match:
        select_table, select_field, from_table, group_table, group_field = group_match.groups()
        if (
            select_table.lower() == from_table.lower()
            and group_table.lower() == from_table.lower()
            and select_field.lower() == group_field.lower()
        ):
            return SqlShape(kind="group_count", table=from_table, field=select_field)
        return None
    avg_match = AVG_RE.match(sql)
    if avg_match:
        metric_table, metric_field, from_table = avg_match.groups()
        if metric_table.lower() == from_table.lower():
            return SqlShape(kind="avg", table=from_table, field=metric_field)
        return None
    avg_not_null_match = AVG_NOT_NULL_RE.match(sql)
    if avg_not_null_match:
        metric_table, metric_field, from_table, where_table, where_field = (
            avg_not_null_match.groups()
        )
        if (
            metric_table.lower() == from_table.lower() == where_table.lower()
            and metric_field.lower() == where_field.lower()
        ):
            return SqlShape(kind="avg", table=from_table, field=metric_field)
        return None
    return None


def _normalize_generated_identifier_quotes(sql: str) -> str:
    return sql.replace("`", "").replace('"', "")


def _shape_matches_question(
    shape: SqlShape | None,
    *,
    expected_kind: str,
    expected_table: str | None,
    expected_field: str | None,
    expected_literal: str | None,
) -> bool:
    if shape is None or expected_table is None:
        return False
    if shape.kind != expected_kind:
        return False
    if shape.table.lower() != expected_table.lower():
        return False
    if expected_field is not None and (shape.field or "").lower() != expected_field.lower():
        return False
    if expected_literal is not None and shape.literal != expected_literal:
        return False
    return True


def _mysql_connect(
    pymysql: Any,
    db_url: str,
    *,
    database: str | None,
    autocommit: bool,
) -> Any:
    parts = urlsplit(db_url)
    if parts.scheme not in {"mysql", "mariadb"}:
        raise ValueError("MySQL/MariaDB probe URL must use mysql:// or mariadb://")
    if not parts.hostname:
        raise ValueError("MySQL/MariaDB probe URL must include a host")
    username = unquote(parts.username or "")
    if not username:
        raise ValueError("MySQL/MariaDB probe URL must include a username")
    password = unquote(parts.password or "") if parts.password is not None else None
    return pymysql.connect(
        host=parts.hostname,
        port=parts.port or 3306,
        user=username,
        password=password,
        database=database,
        autocommit=autocommit,
        charset="utf8mb4",
    )


def _mysql_url_with_database(db_url: str, database: str) -> str:
    parts = urlsplit(db_url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{database}", parts.query, parts.fragment))


def _database_from_url(db_url: str) -> str | None:
    path = urlsplit(db_url).path.strip("/")
    return unquote(path) if path else None


def _graph_sample_value_count(graph_path: Path) -> int:
    with sqlite3.connect(graph_path) as conn:
        row = conn.execute("SELECT COUNT(*) FROM sample_values").fetchone()
    return int(row[0] if row else 0)


def _graph_sample_values(graph_path: Path) -> dict[str, list[str]]:
    with sqlite3.connect(graph_path) as conn:
        rows = conn.execute(
            "SELECT field_canonical, examples FROM sample_values "
            "WHERE pii_redacted = 0 ORDER BY field_canonical"
        ).fetchall()
    out: dict[str, list[str]] = {}
    for field_canonical, examples_json in rows:
        try:
            values = json.loads(str(examples_json))
        except json.JSONDecodeError:
            continue
        if isinstance(values, list):
            out[str(field_canonical)] = [
                str(value)
                for value in values
                if value is not None and _safe_filter_sample_value(str(value))
            ]
    return out


def _safe_filter_sample_value(value: str) -> bool:
    text = value.strip()
    if not text or len(text) > 40:
        return False
    lower = text.lower()
    if lower in {"null", "none", "n/a", "na", "unknown"}:
        return False
    if any(token in lower for token in ("@", "://", "<", ">", "\n", "\r", "\t")):
        return False
    if re.search(r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b", text):
        return False
    if re.fullmatch(r"[a-f0-9]{16,}", lower):
        return False
    if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f-]{27,}", lower):
        return False
    if re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        return False
    if text.count(" ") > 4:
        return False
    return re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _./:-]*", text) is not None


def _skipped_report(
    *, reason: str, detail: str, out_dir: Path, seed: int, engine: str = "mysql"
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "engine": engine,
        "status": "skipped",
        "skip_reason": reason,
        "skip_detail": detail,
        "seed": seed,
        "database": None,
        "out_dir": str(out_dir),
        "summary": {
            "questions": 0,
            "required_questions": 0,
            "required_ok": 0,
            "required_needs_review": 0,
            "routed": 0,
            "count_only_routes": 0,
            "executed_count_only_queries": 0,
            "analytics_questions": 0,
            "analytics_ok": 0,
            "executed_governed_analytics_queries": 0,
            "analytics_needs_review": 0,
            "execution_errors": 0,
            "safe_not_executed": 0,
            "semantic_ok_or_safe_not_executed": 0,
            "needs_review": 0,
            "sample_value_rows": 0,
            "stages": {},
            "pass": False,
            "skipped": True,
        },
        "records": [],
    }


def _error_report(
    *,
    out_dir: Path,
    seed: int,
    database: str | None,
    error: Exception,
    engine: str = "mysql",
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "engine": engine,
        "status": "error",
        "error_detail": str(error),
        "seed": seed,
        "database": database,
        "out_dir": str(out_dir),
        "summary": {
            "questions": 0,
            "required_questions": 0,
            "required_ok": 0,
            "required_needs_review": 0,
            "routed": 0,
            "count_only_routes": 0,
            "executed_count_only_queries": 0,
            "analytics_questions": 0,
            "analytics_ok": 0,
            "executed_governed_analytics_queries": 0,
            "analytics_needs_review": 0,
            "execution_errors": 0,
            "safe_not_executed": 0,
            "semantic_ok_or_safe_not_executed": 0,
            "needs_review": 0,
            "sample_value_rows": 0,
            "stages": {},
            "pass": False,
            "skipped": False,
        },
        "records": [],
    }


def humanize_identifier(value: str) -> str:
    parts = re.split(r"[^A-Za-z0-9]+", value)
    return " ".join(part for part in parts if part).lower() or value.lower()


def name_looks_sensitive(value: str) -> bool:
    lower = value.lower()
    compact = "".join(ch for ch in lower if ch.isalnum())
    needles = [
        "password",
        "token",
        "secret",
        "credential",
        "session",
        "oauth",
        "apikey",
        "accesskey",
        "privatekey",
        "id_number",
        "identitynumber",
        "nationalid",
        "hash",
        "salt",
        "pin",
        "mfa",
        "twofactor",
        "authentication",
        "login",
        "kyc",
        "nin",
        "ssn",
        "taxid",
        "tax_id",
    ]
    return any(needle in lower or needle in compact for needle in needles)


def redact_db_url(db_url: str) -> str:
    parts = urlsplit(db_url)
    if parts.password is None:
        return db_url
    username = unquote(parts.username or "")
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    netloc = f"{username}:***@{host}{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def html_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def field_ref(table: Any, field: Any) -> str:
    if table is None:
        return ""
    if field is None:
        return str(table)
    return f"{table}.{field}"
