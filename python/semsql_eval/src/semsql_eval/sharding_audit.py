"""Schema-only sharding audit for MySQL/MariaDB databases.

The audit reads information_schema metadata and optional Laravel source hints.
It never samples table data and never executes application-table queries.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any

from .realdb_schema_probe import _database_from_url, _mysql_connect, redact_db_url

SHARD_SUFFIX_RE = re.compile(
    r"^(?P<base>[A-Za-z][A-Za-z0-9_]*?)_(?P<anchor>[A-Za-z][A-Za-z0-9_]*)_(?P<id>[0-9]+)$"
)
DEFAULT_ANCHORS = {
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


@dataclass(frozen=True)
class TableMeta:
    name: str
    rows: int | None


@dataclass(frozen=True)
class ColumnMeta:
    table: str
    name: str
    column_type: str
    nullable: str
    key: str


@dataclass(frozen=True)
class ShardParts:
    base: str
    anchor: str
    shard_id: str


def run_mysql_sharding_audit(
    *,
    db_url: str | None = None,
    database: str | None = None,
    source_root: Path | None = None,
) -> dict[str, Any]:
    """Audit table names/source hints for shard families."""
    resolved_url = db_url or os.environ.get("SEMSQL_MYSQL_PROBE_URL")
    if not resolved_url:
        return _skipped_report("missing_db_url", "pass --db-url or set SEMSQL_MYSQL_PROBE_URL")
    try:
        pymysql = import_module("pymysql")
    except ImportError:
        return _skipped_report(
            "missing_pymysql",
            "run with `uv run --extra db ...` or install pymysql",
        )
    selected_database = database or _database_from_url(resolved_url)
    if selected_database is None:
        return _skipped_report(
            "missing_database",
            "pass --database or include a database name in --db-url",
        )

    conn = _mysql_connect(
        pymysql,
        resolved_url,
        database="information_schema",
        autocommit=True,
    )
    try:
        tables = _list_tables(conn, selected_database)
        columns = _list_columns(conn, selected_database)
    finally:
        conn.close()

    source = inspect_laravel_sharding_source(source_root) if source_root else None
    report = audit_sharding_metadata(
        database=selected_database,
        tables=tables,
        columns=columns,
        source=source,
    )
    report["database_url_redacted"] = redact_db_url(resolved_url)
    return report


def audit_sharding_metadata(
    *,
    database: str,
    tables: list[TableMeta],
    columns: list[ColumnMeta],
    source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    table_names = [table.name for table in tables]
    table_set = set(table_names)
    row_counts = {table.name: table.rows for table in tables}
    by_table_columns: dict[str, list[ColumnMeta]] = defaultdict(list)
    for column in columns:
        by_table_columns[column.table].append(column)

    anchor_candidates = _anchor_candidates(table_names, source)
    shard_map: dict[tuple[str, str], list[ShardParts]] = defaultdict(list)
    malformed_tables: list[dict[str, Any]] = []
    unanchored_suffix_tables: list[str] = []
    for table in table_names:
        parts = _parse_shard_table(table, anchor_candidates)
        if parts is not None:
            shard_map[(parts.base, parts.anchor)].append(parts)
            continue
        malformed = _parse_malformed_shard_table(table, anchor_candidates)
        if malformed is not None:
            malformed_tables.append(
                {
                    "table": table,
                    "base": malformed.base,
                    "anchor": malformed.anchor,
                    "reason": "missing_shard_id",
                }
            )
        elif SHARD_SUFFIX_RE.match(table):
            unanchored_suffix_tables.append(table)

    families = []
    nested_shards: list[str] = []
    for (base, anchor), parts_list in sorted(shard_map.items()):
        shard_tables = sorted(part.base + "_" + part.anchor + "_" + part.shard_id for part in parts_list)
        if _parse_shard_table(base, anchor_candidates) is not None:
            nested_shards.extend(shard_tables)
        family = _family_report(
            base=base,
            anchor=anchor,
            shard_tables=shard_tables,
            table_set=table_set,
            row_counts=row_counts,
            columns_by_table=by_table_columns,
        )
        families.append(family)

    source_expected = set(source.get("expected_base_tables", [])) if source else set()
    db_family_bases = {str(family["base_table"]) for family in families}
    source_missing_families = sorted(source_expected - db_family_bases)
    source_extra_families = sorted(db_family_bases - source_expected) if source_expected else []

    problem_count = (
        len(malformed_tables)
        + len(nested_shards)
        + sum(1 for family in families if family["needs_review"])
        + len(source_missing_families)
    )
    status = "pass"
    if families:
        status = "review"
    if problem_count:
        status = "review"
    return {
        "schema_version": 1,
        "engine": "mysql",
        "status": status,
        "database": database,
        "source": source,
        "summary": {
            "table_count": len(tables),
            "shard_family_count": len(families),
            "shard_table_count": sum(len(family["shards"]) for family in families),
            "active_shard_table_count": sum(
                len(family.get("active_shard_tables", [])) for family in families
            ),
            "active_ambiguous_family_count": sum(
                1
                for family in families
                if int(family.get("active_physical_table_count", 0)) > 1
            ),
            "malformed_shard_table_count": len(malformed_tables),
            "nested_shard_table_count": len(nested_shards),
            "unanchored_suffix_table_count": len(unanchored_suffix_tables),
            "source_expected_family_count": len(source_expected),
            "source_missing_family_count": len(source_missing_families),
            "source_extra_family_count": len(source_extra_families),
            "needs_review": problem_count,
            "pass": problem_count == 0 and not families,
        },
        "families": families,
        "malformed_shard_tables": malformed_tables,
        "nested_shard_tables": sorted(nested_shards),
        "unanchored_suffix_tables": sorted(unanchored_suffix_tables),
        "source_missing_families": source_missing_families,
        "source_extra_families": source_extra_families,
        "safety_note": "information_schema/source-only audit; no table data sampled",
    }


def render_mysql_sharding_audit_markdown(report: dict[str, Any]) -> str:
    status = str(report.get("status", "unknown")).upper()
    summary = report["summary"]
    lines = [
        "# MySQL/MariaDB Sharding Audit",
        "",
        f"- status: `{status}`",
        f"- database: `{report.get('database')}`",
        f"- safety: `{report.get('safety_note')}`",
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

    lines.extend(
        [
            "## Summary",
            "",
            f"- tables: `{summary['table_count']}`",
            f"- shard families: `{summary['shard_family_count']}`",
            f"- shard tables: `{summary['shard_table_count']}`",
            f"- active shard tables: `{summary.get('active_shard_table_count', 0)}`",
            f"- active ambiguous families: `{summary.get('active_ambiguous_family_count', 0)}`",
            f"- malformed shard tables: `{summary['malformed_shard_table_count']}`",
            f"- nested shard tables: `{summary['nested_shard_table_count']}`",
            f"- source expected families: `{summary['source_expected_family_count']}`",
            f"- needs review: `{summary['needs_review']}`",
            "",
        ]
    )
    source = report.get("source")
    if isinstance(source, dict):
        lines.extend(
            [
                "## Source Hints",
                "",
                f"- source inspected: `{source.get('inspected')}`",
                f"- configured shard models: `{', '.join(source.get('configured_models', []))}`",
                f"- expected base tables: `{', '.join(source.get('expected_base_tables', []))}`",
                "",
            ]
        )
    lines.extend(
        [
            "## Families",
            "",
            (
                "| Base | Anchor | Shards | Rows | Column drift | "
                "Active physical | Missing cols | Extra cols | Review |"
            ),
            "|---|---|---:|---:|---:|---|---|---|---|",
        ]
    )
    for family in report["families"]:
        review_reasons = ", ".join(family["review_reasons"])
        active_physical = ", ".join(family.get("active_physical_tables", [])) or "-"
        lines.append(
            f"| `{family['base_table']}` | `{family['anchor_table']}` | "
            f"`{len(family['shards'])}` | `{family['approx_rows_total']}` | "
            f"`{family['type_drift_count']}` | "
            f"`{active_physical}` | "
            f"`{', '.join(family['missing_columns_sample'])}` | "
            f"`{', '.join(family['extra_columns_sample'])}` | "
            f"`{review_reasons}` |"
        )
    lines.append("")
    if report["malformed_shard_tables"]:
        lines.extend(["## Malformed Shard Tables", ""])
        for row in report["malformed_shard_tables"]:
            lines.append(
                f"- `{row['table']}`: base `{row['base']}`, anchor `{row['anchor']}`, "
                f"reason `{row['reason']}`"
            )
        lines.append("")
    if report["nested_shard_tables"]:
        lines.extend(["## Nested Shard Tables", ""])
        for table in report["nested_shard_tables"]:
            lines.append(f"- `{table}`")
        lines.append("")
    if report["source_missing_families"]:
        lines.extend(["## Source Families Missing From DB", ""])
        for base in report["source_missing_families"]:
            lines.append(f"- `{base}`")
        lines.append("")
    if report["source_extra_families"]:
        lines.extend(["## DB Families Not Listed In Source Config", ""])
        for base in report["source_extra_families"]:
            lines.append(f"- `{base}`")
        lines.append("")
    return "\n".join(lines)


def inspect_laravel_sharding_source(source_root: Path | None) -> dict[str, Any]:
    if source_root is None or not source_root.exists():
        return {
            "inspected": False,
            "configured_models": [],
            "expected_base_tables": [],
            "reason": "missing_source_root",
        }
    config = source_root / "config" / "sharding.php"
    configured_models: list[str] = []
    if config.exists():
        text = config.read_text(encoding="utf-8", errors="ignore")
        configured_models = re.findall(r"App\\Models\\([A-Za-z0-9_]+)::class", text)
    expected = sorted({_model_to_table(model) for model in configured_models})
    shardable_models = []
    model_root = source_root / "app" / "Models"
    if model_root.exists():
        for path in model_root.glob("*.php"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "use Shardable" in text:
                shardable_models.append(path.stem)
    return {
        "inspected": True,
        "source_name": source_root.name,
        "configured_models": sorted(configured_models),
        "expected_base_tables": expected,
        "shardable_model_files": sorted(shardable_models),
    }


def _list_tables(conn: Any, database: str) -> list[TableMeta]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name, table_rows
            FROM information_schema.tables
            WHERE table_schema = %s
              AND table_type = 'BASE TABLE'
            ORDER BY table_name
            """,
            (database,),
        )
        return [
            TableMeta(name=str(row[0]), rows=int(row[1]) if row[1] is not None else None)
            for row in cur.fetchall()
        ]


def _list_columns(conn: Any, database: str) -> list[ColumnMeta]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name, column_name, column_type, is_nullable, column_key
            FROM information_schema.columns
            WHERE table_schema = %s
            ORDER BY table_name, ordinal_position
            """,
            (database,),
        )
        return [
            ColumnMeta(
                table=str(row[0]),
                name=str(row[1]),
                column_type=str(row[2]),
                nullable=str(row[3]),
                key=str(row[4] or ""),
            )
            for row in cur.fetchall()
        ]


def _family_report(
    *,
    base: str,
    anchor: str,
    shard_tables: list[str],
    table_set: set[str],
    row_counts: dict[str, int | None],
    columns_by_table: dict[str, list[ColumnMeta]],
) -> dict[str, Any]:
    base_exists = base in table_set
    base_approx_rows = row_counts.get(base) if base_exists else None
    base_cols = {column.name: column for column in columns_by_table.get(base, [])}
    missing_columns: set[str] = set()
    extra_columns: set[str] = set()
    type_drifts: list[str] = []
    review_reasons = []
    active_shard_tables = [
        shard for shard in shard_tables if (row_counts.get(shard) or 0) > 0
    ]
    active_physical_tables = [
        table
        for table in ([base] if (base_approx_rows or 0) > 0 else []) + active_shard_tables
    ]
    for shard in shard_tables:
        shard_cols = {column.name: column for column in columns_by_table.get(shard, [])}
        if base_cols:
            missing_columns.update(sorted(set(base_cols) - set(shard_cols)))
            extra_columns.update(sorted(set(shard_cols) - set(base_cols)))
            for name in sorted(set(base_cols) & set(shard_cols)):
                if _normalized_column_type(base_cols[name]) != _normalized_column_type(shard_cols[name]):
                    type_drifts.append(f"{shard}.{name}")
    if not base_exists:
        review_reasons.append("missing_base_table")
    if missing_columns:
        review_reasons.append("missing_columns")
    if extra_columns:
        review_reasons.append("extra_columns")
    if type_drifts:
        review_reasons.append("type_drift")
    if len(active_physical_tables) > 1:
        review_reasons.append("active_table_ambiguity")
    return {
        "base_table": base,
        "anchor_table": anchor,
        "base_exists": base_exists,
        "base_approx_rows": base_approx_rows,
        "shards": [
            {
                "table": shard,
                "shard_id": shard.rsplit("_", 1)[-1],
                "approx_rows": row_counts.get(shard),
            }
            for shard in shard_tables
        ],
        "active_shard_tables": active_shard_tables,
        "active_physical_tables": active_physical_tables,
        "active_physical_table_count": len(active_physical_tables),
        "approx_rows_total": sum(row_counts.get(shard) or 0 for shard in shard_tables),
        "missing_columns_count": len(missing_columns),
        "missing_columns_sample": sorted(missing_columns)[:8],
        "extra_columns_count": len(extra_columns),
        "extra_columns_sample": sorted(extra_columns)[:8],
        "type_drift_count": len(type_drifts),
        "type_drift_sample": type_drifts[:12],
        "needs_review": bool(review_reasons),
        "review_reasons": review_reasons,
    }


def _anchor_candidates(table_names: list[str], source: dict[str, Any] | None) -> list[str]:
    anchors = set(DEFAULT_ANCHORS)
    anchors.update(table_names)
    if source:
        for table in source.get("expected_anchor_tables", []):
            anchors.add(str(table))
    return sorted(anchors, key=lambda value: (-len(value), value))


def _parse_shard_table(table: str, anchors: list[str]) -> ShardParts | None:
    for anchor in anchors:
        marker = f"_{anchor}_"
        if marker not in table:
            continue
        base, shard_id = table.rsplit(marker, 1)
        if base and shard_id.isdigit():
            return ShardParts(base=base, anchor=anchor, shard_id=shard_id)
    return None


def _parse_malformed_shard_table(table: str, anchors: list[str]) -> ShardParts | None:
    for anchor in anchors:
        marker = f"_{anchor}_"
        if table.endswith(marker):
            base = table[: -len(marker)]
            if base:
                return ShardParts(base=base, anchor=anchor, shard_id="")
    return None


def _normalized_column_type(column: ColumnMeta) -> str:
    column_type = column.column_type.lower()
    if column_type.startswith(("varchar", "char", "text", "mediumtext", "longtext")):
        return "text"
    if column_type.startswith(("tinyint(1)", "boolean", "bool")):
        return "boolean"
    if column_type.startswith(("tinyint", "int", "bigint", "smallint", "mediumint")):
        return "integer"
    if column_type.startswith(("decimal", "float", "double")):
        return "float"
    if column_type.startswith(("datetime", "timestamp", "date", "time")):
        return "temporal"
    return column_type


def _model_to_table(model: str) -> str:
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", model).lower()
    if snake.endswith("alias"):
        return snake + "es"
    if snake.endswith("y"):
        return snake[:-1] + "ies"
    if snake.endswith("s"):
        return snake
    return snake + "s"


def _skipped_report(reason: str, detail: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "engine": "mysql",
        "status": "skipped",
        "skip_reason": reason,
        "skip_detail": detail,
        "summary": {
            "table_count": 0,
            "shard_family_count": 0,
            "shard_table_count": 0,
            "active_shard_table_count": 0,
            "active_ambiguous_family_count": 0,
            "malformed_shard_table_count": 0,
            "nested_shard_table_count": 0,
            "unanchored_suffix_table_count": 0,
            "source_expected_family_count": 0,
            "source_missing_family_count": 0,
            "source_extra_family_count": 0,
            "needs_review": 0,
            "pass": False,
            "skipped": True,
        },
        "families": [],
        "malformed_shard_tables": [],
        "nested_shard_tables": [],
        "unanchored_suffix_tables": [],
        "source_missing_families": [],
        "source_extra_families": [],
        "safety_note": "information_schema/source-only audit; no table data sampled",
    }
