"""Practical assessment for a mini SemanticAtlas direction.

This is eval-only. It does not change runtime behavior and it does not use gold
SQL to build candidates. Gold SQL is used only after binding to score whether a
raw schema/sample atlas or a small semantic atlas has enough evidence to build a
typed plan.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

import sqlglot
from sqlglot import exp

from .platform_suite import build_business_analytics_suite, build_platform_query_suite

AtlasMode = Literal["raw", "semantic"]
SuiteName = Literal["platform", "business"]

__all__ = [
    "render_semantic_atlas_assessment_markdown",
    "run_semantic_atlas_assessment",
]


@dataclass(frozen=True)
class AtlasColumn:
    table: str
    name: str
    sql_type: str

    @property
    def canonical(self) -> str:
        return f"{self.table}.{self.name}"


@dataclass(frozen=True)
class AtlasValueHit:
    mention: str
    field: str
    value: str
    source: str


@dataclass(frozen=True)
class GoldBinding:
    field: str
    value: str
    operator: str


@dataclass(frozen=True)
class GoldEvidence:
    parse_ok: bool
    tables: set[str]
    columns: set[str]
    bindings: list[GoldBinding]
    aggregate_ops: set[str]
    has_group_by: bool
    has_order_by: bool
    has_date_range: bool
    has_in_list: bool
    has_null_predicate: bool
    has_not_exists: bool
    has_expression_metric: bool


@dataclass
class MiniSemanticAtlas:
    db_id: str
    sqlite_path: Path
    mode: AtlasMode
    schema_notes: dict[str, str]
    tables: set[str]
    columns: dict[str, AtlasColumn]
    table_columns: dict[str, list[AtlasColumn]]
    table_aliases: dict[str, set[str]]
    field_aliases: dict[str, set[str]]
    table_graph: dict[str, set[str]]
    samples: dict[str, list[str]]
    metric_aliases: dict[str, set[str]] = field(default_factory=dict)

    @classmethod
    def load(
        cls,
        *,
        db_id: str,
        sqlite_path: Path,
        schema_notes: dict[str, str],
        mode: AtlasMode,
    ) -> MiniSemanticAtlas:
        tables, columns, table_columns, graph = _read_sqlite_schema(sqlite_path)
        samples = _read_samples(sqlite_path, table_columns)
        table_aliases = _table_aliases(tables, schema_notes, mode)
        field_aliases = _field_aliases(columns, table_aliases, mode)
        metric_aliases = _metric_aliases(columns) if mode == "semantic" else {}
        return cls(
            db_id=db_id,
            sqlite_path=sqlite_path,
            mode=mode,
            schema_notes=schema_notes,
            tables=tables,
            columns=columns,
            table_columns=table_columns,
            table_aliases=table_aliases,
            field_aliases=field_aliases,
            table_graph=graph,
            samples=samples,
            metric_aliases=metric_aliases,
        )

    def candidate_tables(self, question: str) -> set[str]:
        matched = {
            table
            for table, aliases in self.table_aliases.items()
            if _any_phrase_matches(question, aliases)
        }
        for field in self.candidate_fields(question):
            table, _ = field.split(".", 1)
            matched.add(table)
        for hit in self.value_hits(question):
            table, _ = hit.field.split(".", 1)
            matched.add(table)
        return matched

    def candidate_fields(self, question: str) -> set[str]:
        matched = {
            field
            for field, aliases in self.field_aliases.items()
            if _any_phrase_matches(question, aliases)
        }
        for metric, fields in self.metric_aliases.items():
            if _phrase_matches(question, metric):
                matched.update(fields)
        if self.mode == "semantic":
            matched.update(_role_fields_from_question(question, self.columns))
        return matched

    def value_hits(self, question: str) -> list[AtlasValueHit]:
        hits: list[AtlasValueHit] = []
        qnorm = _normalize_phrase(question)
        for field, values in self.samples.items():
            for value in values:
                if _sample_value_mentions_question(qnorm, value):
                    hits.append(
                        AtlasValueHit(
                            mention=value,
                            field=field,
                            value=value,
                            source="sample",
                        )
                    )
        if self.mode == "semantic":
            hits.extend(self._semantic_literal_hits(question))
            hits.extend(self._semantic_scope_hits(question))
        return _dedupe_value_hits(hits)

    def has_join_path(self, left: str, right: str, max_hops: int = 4) -> bool:
        if left == right:
            return True
        todo: deque[tuple[str, int]] = deque([(left, 0)])
        seen = {left}
        while todo:
            table, hops = todo.popleft()
            if hops >= max_hops:
                continue
            for nxt in self.table_graph.get(table, set()):
                if nxt == right:
                    return True
                if nxt not in seen:
                    seen.add(nxt)
                    todo.append((nxt, hops + 1))
        return False

    def _semantic_scope_hits(self, question: str) -> list[AtlasValueHit]:
        hits: list[AtlasValueHit] = []
        qtokens = _tokens(question)
        for field, values in self.samples.items():
            lower_values = {_normalize_phrase(v) for v in values}
            if not lower_values:
                continue
            col = self.columns[field]
            name = col.name.lower()
            if "active" in qtokens and "active" in lower_values:
                hits.append(AtlasValueHit("active", field, "active", "semantic_scope"))
            if "inactive" in qtokens and any(v in lower_values for v in {"0", "inactive"}):
                value = "0" if "0" in lower_values else "inactive"
                hits.append(AtlasValueHit("inactive", field, value, "semantic_scope"))
            if "open" in qtokens and "open" in lower_values:
                hits.append(AtlasValueHit("open", field, "open", "semantic_scope"))
            if "paid" in qtokens and "paid" in lower_values:
                hits.append(AtlasValueHit("paid", field, "paid", "semantic_scope"))
            if "overdue" in qtokens and "overdue" in lower_values:
                hits.append(AtlasValueHit("overdue", field, "overdue", "semantic_scope"))
            if "resolved" in qtokens and "resolved" in lower_values:
                hits.append(AtlasValueHit("resolved", field, "resolved", "semantic_scope"))
            if "enterprise" in qtokens and "enterprise" in lower_values:
                hits.append(AtlasValueHit("enterprise", field, "enterprise", "semantic_scope"))
            if {"high", "priority"} <= qtokens and "high" in lower_values:
                hits.append(AtlasValueHit("high priority", field, "high", "semantic_scope"))
            if "customer" in qtokens and "customer" in lower_values:
                hits.append(AtlasValueHit("customer", field, "customer", "semantic_scope"))
            if "churned" in qtokens and "churned" in lower_values:
                hits.append(AtlasValueHit("churned", field, "churned", "semantic_scope"))
            if "cancel" in qtokens or "cancellation" in qtokens:
                if "cancelled" in lower_values:
                    hits.append(
                        AtlasValueHit("cancellation", field, "cancelled", "semantic_scope")
                    )
            if "breach" in qtokens and "sla" in qtokens and _field_role(col) == "boolean":
                hits.append(AtlasValueHit("SLA breach", field, "1", "semantic_scope"))
            if name.endswith("_rep_id") or name.endswith("_agent_id"):
                continue
        return hits

    def _semantic_literal_hits(self, question: str) -> list[AtlasValueHit]:
        hits: list[AtlasValueHit] = []
        for literal in _structured_literals(question):
            for field, col in self.columns.items():
                if _literal_compatible_with_field(question, literal, col, self.field_aliases[field]):
                    hits.append(AtlasValueHit(literal, field, literal, "structured_literal"))
        return hits


def run_semantic_atlas_assessment(
    *,
    out_dir: Path,
    suites: tuple[SuiteName, ...] = ("platform", "business"),
) -> dict[str, Any]:
    """Run raw-vs-semantic atlas assessment over practical suites."""
    out_dir.mkdir(parents=True, exist_ok=True)
    case_rows: list[dict[str, Any]] = []
    suite_reports: list[dict[str, Any]] = []
    for suite_name in suites:
        suite_out = out_dir / suite_name
        suite = (
            build_platform_query_suite(suite_out)
            if suite_name == "platform"
            else build_business_analytics_suite(suite_out)
        )
        sqlite_path = Path(str(suite["sqlite_path"]))
        db_id = str(suite["db_id"])
        raw_notes = suite.get("schema_notes")
        notes = (
            {str(key): str(value) for key, value in raw_notes.items()}
            if isinstance(raw_notes, dict)
            else {}
        )
        raw = MiniSemanticAtlas.load(
            db_id=db_id,
            sqlite_path=sqlite_path,
            schema_notes=notes,
            mode="raw",
        )
        semantic = MiniSemanticAtlas.load(
            db_id=db_id,
            sqlite_path=sqlite_path,
            schema_notes=notes,
            mode="semantic",
        )
        raw_cases = suite.get("cases")
        cases = (
            [case for case in raw_cases if isinstance(case, dict)]
            if isinstance(raw_cases, list | tuple)
            else []
        )
        for case in cases:
            case_rows.append(_assess_case(suite_name, case, raw, semantic))
        suite_reports.append(
            {
                "suite": suite_name,
                "db_id": db_id,
                "sqlite_path": str(sqlite_path),
                "cases": len(cases),
            }
        )

    return {
        "schema_version": 1,
        "assessment": "mini-semantic-atlas-v1",
        "out_dir": str(out_dir),
        "suites": suite_reports,
        "summary": _summarise(case_rows),
        "cases": case_rows,
    }


def render_semantic_atlas_assessment_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# Mini SemanticAtlas Practical Assessment",
        "",
        f"- assessment: `{report['assessment']}`",
        f"- route cases: `{summary['route_total']}`",
        f"- non-route cases: `{summary['nonroute_total']}`",
        "",
        "## Raw vs Mini Semantic Atlas",
        "",
        "| mode | route plan-ready | non-route fail-closed | wrong-accept risk | table recall | field recall | value-field recall | intent hit | date hit | metric hit |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for mode in ("raw", "semantic"):
        row = summary["modes"][mode]
        lines.append(
            "| `{mode}` | `{ready}/{route}` `{ready_rate:.2%}` | `{closed}/{nonroute}` `{closed_rate:.2%}` | `{risk}` | `{table:.2%}` | `{field:.2%}` | `{value:.2%}` | `{intent:.2%}` | `{date:.2%}` | `{metric:.2%}` |".format(
                mode=mode,
                ready=row["route_plan_ready"],
                route=summary["route_total"],
                ready_rate=row["route_plan_ready_rate"],
                closed=row["nonroute_fail_closed"],
                nonroute=summary["nonroute_total"],
                closed_rate=row["nonroute_fail_closed_rate"],
                risk=row["wrong_accept_risk"],
                table=row["table_recall_avg"],
                field=row["field_recall_avg"],
                value=row["value_field_recall_avg"],
                intent=row["intent_hit_rate"],
                date=row["date_window_hit_rate"],
                metric=row["metric_hit_rate"],
            )
        )
    delta = summary["semantic_lift"]
    lines.extend(
        [
            "",
            "## Lift",
            "",
            f"- route plan-ready lift: `{delta['route_plan_ready_delta']}` cases",
            f"- wrong-accept risk delta: `{delta['wrong_accept_risk_delta']}` cases",
            f"- field recall lift: `{delta['field_recall_delta']:.2%}`",
            f"- value-field recall lift: `{delta['value_field_recall_delta']:.2%}`",
            "",
            "## Semantic Plan-Ready By Family",
            "",
            "| family | route cases | semantic ready | raw ready |",
            "|---|---:|---:|---:|",
        ]
    )
    for family, stats in summary["by_family"].items():
        lines.append(
            f"| `{family}` | `{stats['route_cases']}` | `{stats['semantic_ready']}` | `{stats['raw_ready']}` |"
        )
    lines.extend(
        [
            "",
            "## Remaining Semantic Gaps",
            "",
        ]
    )
    for reason, count in summary["semantic_gap_reasons"].items():
        lines.append(f"- `{reason}`: `{count}`")
    lines.extend(
        [
            "",
            "## Case Matrix",
            "",
            "| suite | id | disposition | family | raw | semantic | semantic gaps |",
            "|---|---|---|---|---|---|---|",
        ]
    )
    for case in report["cases"]:
        lines.append(
            "| `{suite}` | `{id}` | `{disp}` | `{family}` | `{raw}` | `{semantic}` | {gaps} |".format(
                suite=case["suite"],
                id=case["id"],
                disp=case["disposition"],
                family=case["family"],
                raw=_case_bucket(case, "raw"),
                semantic=_case_bucket(case, "semantic"),
                gaps=", ".join(f"`{g}`" for g in case["modes"]["semantic"]["gap_reasons"])
                or "-",
            )
        )
    lines.extend(
        [
            "",
            "## Read",
            "",
            "This is an evidence and typed-plan readiness assessment, not a runtime SQL benchmark.",
            "The mini atlas uses schema names, table notes, sample values, reusable field-role aliases,",
            "date/value typing, and a tiny governed metric catalog. Gold SQL is used only for scoring.",
            "A plan-ready route case still needs the runtime `BoundQueryPlan` compiler before it can",
            "safely execute in production.",
            "",
        ]
    )
    return "\n".join(lines)


def _assess_case(
    suite_name: SuiteName,
    case: dict[str, Any],
    raw: MiniSemanticAtlas,
    semantic: MiniSemanticAtlas,
) -> dict[str, Any]:
    gold = _extract_gold_evidence(str(case.get("expected_sql") or ""), semantic)
    row = {
        "suite": suite_name,
        "id": str(case["id"]),
        "question": str(case["question"]),
        "disposition": str(case["disposition"]),
        "family": str(case["family"]),
        "difficulty": str(case["difficulty"]),
        "modes": {
            "raw": _assess_mode(case, raw, gold),
            "semantic": _assess_mode(case, semantic, gold),
        },
    }
    return row


def _assess_mode(
    case: dict[str, Any],
    atlas: MiniSemanticAtlas,
    gold: GoldEvidence,
) -> dict[str, Any]:
    question = str(case["question"])
    disposition = str(case["disposition"])
    nonroute = disposition != "route"
    predicted_nonroute = _classify_nonroute(question, atlas)
    if nonroute:
        return {
            "plan_ready": False,
            "fail_closed": predicted_nonroute is not None,
            "wrong_accept_risk": predicted_nonroute is None,
            "nonroute_reason": predicted_nonroute,
            "table_recall": 1.0,
            "field_recall": 1.0,
            "value_field_recall": 1.0,
            "intent_hit": True,
            "date_window_hit": True,
            "metric_hit": True,
            "gap_reasons": [] if predicted_nonroute else ["nonroute_not_classified"],
            "candidate_tables": [],
            "candidate_fields": [],
            "value_hits": [],
        }

    candidate_tables = atlas.candidate_tables(question)
    candidate_fields = atlas.candidate_fields(question)
    value_hits = atlas.value_hits(question)
    table_recall = _recall(gold.tables, candidate_tables)
    field_recall = _recall(_plan_relevant_fields(gold, atlas), candidate_fields)
    value_field_recall = _value_field_recall(gold.bindings, value_hits)
    join_path_hit = _join_path_hit(gold, atlas)
    intent_hit = _intent_hit(question, gold, atlas)
    date_window_hit = _date_window_hit(question, gold, atlas)
    metric_hit = _metric_hit(question, gold, atlas)
    feature_supported, unsupported = _features_supported(gold, atlas)
    gap_reasons: list[str] = []
    if not gold.parse_ok:
        gap_reasons.append("gold_parse_failed")
    if table_recall < 0.70:
        gap_reasons.append("low_table_recall")
    if field_recall < 0.35:
        gap_reasons.append("low_field_recall")
    effective_value_field_recall = (
        1.0 if gold.has_expression_metric and metric_hit and atlas.mode == "semantic" else value_field_recall
    )
    if effective_value_field_recall < 0.60:
        gap_reasons.append("low_value_field_recall")
    if join_path_hit is False:
        gap_reasons.append("missing_join_path")
    if not intent_hit:
        gap_reasons.append("intent_not_detected")
    if not date_window_hit:
        gap_reasons.append("date_window_not_grounded")
    if not metric_hit:
        gap_reasons.append("metric_not_cataloged")
    gap_reasons.extend(unsupported)
    plan_ready = not gap_reasons and feature_supported
    return {
        "plan_ready": plan_ready,
        "fail_closed": False,
        "wrong_accept_risk": False,
        "nonroute_reason": None,
        "table_recall": table_recall,
        "field_recall": field_recall,
        "value_field_recall": effective_value_field_recall,
        "intent_hit": intent_hit,
        "date_window_hit": date_window_hit,
        "metric_hit": metric_hit,
        "join_path_hit": join_path_hit,
        "gap_reasons": gap_reasons,
        "candidate_tables": sorted(candidate_tables),
        "candidate_fields": sorted(candidate_fields),
        "value_hits": [
            {"field": hit.field, "mention": hit.mention, "value": hit.value, "source": hit.source}
            for hit in value_hits
        ],
    }


def _summarise(case_rows: list[dict[str, Any]]) -> dict[str, Any]:
    route = [row for row in case_rows if row["disposition"] == "route"]
    nonroute = [row for row in case_rows if row["disposition"] != "route"]
    atlas_modes: tuple[AtlasMode, AtlasMode] = ("raw", "semantic")
    modes = {
        mode: _summarise_mode(case_rows, route, nonroute, mode)
        for mode in atlas_modes
    }
    by_family: dict[str, dict[str, int]] = {}
    for family in sorted({row["family"] for row in route}):
        family_rows = [row for row in route if row["family"] == family]
        by_family[family] = {
            "route_cases": len(family_rows),
            "raw_ready": sum(1 for row in family_rows if row["modes"]["raw"]["plan_ready"]),
            "semantic_ready": sum(
                1 for row in family_rows if row["modes"]["semantic"]["plan_ready"]
            ),
        }
    gap_counts: Counter[str] = Counter()
    for row in route:
        for reason in row["modes"]["semantic"]["gap_reasons"]:
            gap_counts[str(reason)] += 1
    return {
        "cases_total": len(case_rows),
        "route_total": len(route),
        "nonroute_total": len(nonroute),
        "modes": modes,
        "semantic_lift": {
            "route_plan_ready_delta": modes["semantic"]["route_plan_ready"]
            - modes["raw"]["route_plan_ready"],
            "wrong_accept_risk_delta": modes["semantic"]["wrong_accept_risk"]
            - modes["raw"]["wrong_accept_risk"],
            "field_recall_delta": modes["semantic"]["field_recall_avg"]
            - modes["raw"]["field_recall_avg"],
            "value_field_recall_delta": modes["semantic"]["value_field_recall_avg"]
            - modes["raw"]["value_field_recall_avg"],
        },
        "by_family": by_family,
        "semantic_gap_reasons": dict(gap_counts.most_common()),
    }


def _summarise_mode(
    all_rows: list[dict[str, Any]],
    route: list[dict[str, Any]],
    nonroute: list[dict[str, Any]],
    mode: AtlasMode,
) -> dict[str, Any]:
    route_modes = [row["modes"][mode] for row in route]
    nonroute_modes = [row["modes"][mode] for row in nonroute]
    ready = sum(1 for row in route_modes if row["plan_ready"])
    closed = sum(1 for row in nonroute_modes if row["fail_closed"])
    risk = sum(1 for row in nonroute_modes if row["wrong_accept_risk"])
    return {
        "route_plan_ready": ready,
        "route_plan_ready_rate": ready / len(route_modes) if route_modes else 0.0,
        "nonroute_fail_closed": closed,
        "nonroute_fail_closed_rate": closed / len(nonroute_modes) if nonroute_modes else 0.0,
        "wrong_accept_risk": risk,
        "table_recall_avg": _avg([row["table_recall"] for row in route_modes]),
        "field_recall_avg": _avg([row["field_recall"] for row in route_modes]),
        "value_field_recall_avg": _avg(
            [row["value_field_recall"] for row in route_modes]
        ),
        "intent_hit_rate": _avg_bool([row["intent_hit"] for row in route_modes]),
        "date_window_hit_rate": _avg_bool([row["date_window_hit"] for row in route_modes]),
        "metric_hit_rate": _avg_bool([row["metric_hit"] for row in route_modes]),
        "all_cases": len(all_rows),
    }


def _read_sqlite_schema(
    sqlite_path: Path,
) -> tuple[
    set[str],
    dict[str, AtlasColumn],
    dict[str, list[AtlasColumn]],
    dict[str, set[str]],
]:
    conn = sqlite3.connect(f"file:{sqlite_path.resolve()}?mode=ro", uri=True)
    try:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        columns: dict[str, AtlasColumn] = {}
        table_columns: dict[str, list[AtlasColumn]] = {}
        graph: dict[str, set[str]] = {table: set() for table in tables}
        for table in sorted(tables):
            infos: list[AtlasColumn] = []
            for row in conn.execute(f"PRAGMA table_info({_quote_ident(table)})"):
                col = AtlasColumn(table=table, name=str(row[1]), sql_type=str(row[2] or ""))
                infos.append(col)
                columns[col.canonical] = col
            table_columns[table] = infos
        for table in sorted(tables):
            for row in conn.execute(f"PRAGMA foreign_key_list({_quote_ident(table)})"):
                other = str(row[2])
                if other in tables:
                    graph.setdefault(table, set()).add(other)
                    graph.setdefault(other, set()).add(table)
        _add_inferred_edges(table_columns, graph)
        return tables, columns, table_columns, graph
    finally:
        conn.close()


def _read_samples(
    sqlite_path: Path,
    table_columns: dict[str, list[AtlasColumn]],
) -> dict[str, list[str]]:
    conn = sqlite3.connect(f"file:{sqlite_path.resolve()}?mode=ro", uri=True)
    try:
        samples: dict[str, list[str]] = {}
        for table, cols in table_columns.items():
            for col in cols:
                if not _should_sample(col):
                    continue
                sql = (
                    f"SELECT DISTINCT CAST({_quote_ident(col.name)} AS TEXT) "
                    f"FROM {_quote_ident(table)} WHERE {_quote_ident(col.name)} IS NOT NULL LIMIT 200"
                )
                try:
                    rows = conn.execute(sql).fetchall()
                except sqlite3.Error:
                    continue
                values = [str(row[0]).strip() for row in rows if row[0] is not None]
                samples[col.canonical] = [value for value in values if value]
        return samples
    finally:
        conn.close()


def _table_aliases(
    tables: set[str],
    schema_notes: dict[str, str],
    mode: AtlasMode,
) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for table in tables:
        aliases = set(_name_phrases(table))
        if mode == "semantic":
            aliases.update(_tokens_to_phrases(schema_notes.get(table, "")))
            aliases.update(_TABLE_SYNONYMS.get(table, set()))
        out[table] = {alias for alias in aliases if len(alias) >= 2}
    return out


def _field_aliases(
    columns: dict[str, AtlasColumn],
    table_aliases: dict[str, set[str]],
    mode: AtlasMode,
) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for field, col in columns.items():
        aliases = set(_name_phrases(col.name))
        aliases.add(col.name.lower())
        if mode == "semantic":
            aliases.update(_FIELD_SYNONYMS.get(col.name.lower(), set()))
            aliases.update(_role_aliases(col))
            for table_alias in table_aliases.get(col.table, set()):
                if _field_role(col) in {"display", "status", "date", "numeric"}:
                    aliases.add(f"{table_alias} {col.name.replace('_', ' ')}")
        out[field] = {alias for alias in aliases if len(alias) >= 2}
    return out


def _metric_aliases(columns: dict[str, AtlasColumn]) -> dict[str, set[str]]:
    fields = set(columns)
    out: dict[str, set[str]] = {}
    if "leads.status" in fields:
        out["conversion rate"] = {"leads.status"}
    if "tickets.sla_breached" in fields:
        out["sla breach rate"] = {"tickets.sla_breached"}
    if "opportunities.created_on" in fields and "opportunities.close_date" in fields:
        out["sales cycle"] = {"opportunities.created_on", "opportunities.close_date"}
    for field in fields:
        tail = field.split(".", 1)[1].lower()
        if tail in {"arr", "mrr", "health_score", "amount", "resolution_hours"}:
            out[tail.replace("_", " ")] = {field}
    return out


def _extract_gold_evidence(sql: str, atlas: MiniSemanticAtlas) -> GoldEvidence:
    if not sql:
        return GoldEvidence(False, set(), set(), [], set(), False, False, False, False, False, False, False)
    try:
        tree = cast(exp.Expression, sqlglot.parse_one(sql, read="sqlite"))
    except Exception:
        return GoldEvidence(False, set(), set(), [], set(), False, False, False, False, False, False, False)
    aliases: dict[str, str] = {}
    tables: set[str] = set()
    for table in tree.find_all(exp.Table):
        name = table.name
        if name in atlas.tables:
            tables.add(name)
            aliases[table.alias_or_name.lower()] = name
            aliases[name.lower()] = name
    columns: set[str] = set()
    for col in tree.find_all(exp.Column):
        resolved = _resolve_column(col, aliases, atlas)
        if resolved:
            columns.add(resolved)
    bindings = _extract_bindings(tree, aliases, atlas)
    aggregate_ops = {
        node.key.upper()
        for node in tree.walk()
        if isinstance(node, (exp.Avg, exp.Count, exp.Sum, exp.Min, exp.Max))
    }
    values = [binding.value for binding in bindings]
    return GoldEvidence(
        parse_ok=True,
        tables=tables,
        columns=columns,
        bindings=bindings,
        aggregate_ops=aggregate_ops,
        has_group_by=tree.find(exp.Group) is not None,
        has_order_by=tree.find(exp.Order) is not None,
        has_date_range=any(_is_iso_date(value) for value in values),
        has_in_list=tree.find(exp.In) is not None,
        has_null_predicate=tree.find(exp.Is) is not None,
        has_not_exists=tree.find(exp.Exists) is not None and "NOT EXISTS" in sql.upper(),
        has_expression_metric=bool(
            re.search(r"\bCASE\b|/|\bjulianday\s*\(", sql, flags=re.IGNORECASE)
        ),
    )


def _extract_bindings(
    tree: exp.Expression,
    aliases: dict[str, str],
    atlas: MiniSemanticAtlas,
) -> list[GoldBinding]:
    out: list[GoldBinding] = []
    for node in tree.find_all(exp.EQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Like):
        for left, right in (
            (node.args.get("this"), node.args.get("expression")),
            (node.args.get("expression"), node.args.get("this")),
        ):
            col = _first_column(left)
            lit = _first_literal(right)
            if col is None or lit is None:
                continue
            field = _resolve_column(col, aliases, atlas)
            if field:
                out.append(GoldBinding(field, str(lit.this), node.key.upper()))
                break
    for in_node in tree.find_all(exp.In):
        col = _first_column(in_node.args.get("this"))
        if col is None:
            continue
        field = _resolve_column(col, aliases, atlas)
        if not field:
            continue
        for lit in in_node.expressions:
            if isinstance(lit, exp.Literal):
                out.append(GoldBinding(field, str(lit.this), "IN"))
    return _dedupe_bindings(out)


def _resolve_column(
    col: exp.Column,
    aliases: dict[str, str],
    atlas: MiniSemanticAtlas,
) -> str | None:
    name = col.name
    if col.table:
        table = aliases.get(col.table.lower(), col.table)
        key = f"{table}.{name}"
        if key in atlas.columns:
            return key
    matches = [field for field in atlas.columns if field.rsplit(".", 1)[1].lower() == name.lower()]
    return matches[0] if len(matches) == 1 else None


def _classify_nonroute(question: str, atlas: MiniSemanticAtlas) -> str | None:
    lower = question.lower()
    if re.search(r"\b(email|create|delete|update|send|export)\b", lower):
        return "reject_side_effect_or_export"
    if lower.startswith("why ") or " why " in lower:
        return "reject_causal_analysis"
    if "percentage" in lower and not any(
        _phrase_matches(question, metric) for metric in atlas.metric_aliases
    ):
        return "clarify_unsupported_ratio"
    if re.search(r"\b(no|not|without)\b.+\b(after|since|before)\b", lower):
        return "clarify_unsupported_anti_join"
    if "all columns" in lower or ("every " in lower and "all" in lower):
        return "reject_row_dump"
    if lower.strip() in {"show status", "status"}:
        status_fields = [
            field for field, col in atlas.columns.items() if "status" in col.name.lower()
        ]
        return "clarify_ambiguous_field" if len(status_fields) > 1 else None
    if "open things" in lower or "things" in lower:
        return "clarify_ambiguous_entity"
    if any(term in lower for term in ["healthiest", "at risk", "risk"]):
        return "clarify_undefined_metric"
    if "revenue" in lower and not any(
        _phrase_matches(question, metric) for metric in atlas.metric_aliases
    ):
        return "clarify_ambiguous_metric"
    if lower.strip() in {"show pipeline", "pipeline"}:
        return "clarify_ambiguous_metric"
    return None


def _features_supported(gold: GoldEvidence, atlas: MiniSemanticAtlas) -> tuple[bool, list[str]]:
    unsupported: list[str] = []
    if atlas.mode == "raw":
        if gold.has_date_range:
            unsupported.append("raw_no_date_window")
        if gold.has_not_exists:
            unsupported.append("raw_no_anti_join")
        if gold.has_expression_metric:
            unsupported.append("raw_no_expression_metric")
        if gold.has_null_predicate:
            unsupported.append("raw_no_null_predicate")
    else:
        if gold.has_not_exists and not _has_semantic_tables(atlas, {"activities", "events"}):
            unsupported.append("semantic_no_anti_join_anchor")
        if gold.has_expression_metric and not atlas.metric_aliases:
            unsupported.append("semantic_no_metric_catalog")
    return not unsupported, unsupported


def _intent_hit(question: str, gold: GoldEvidence, atlas: MiniSemanticAtlas) -> bool:
    lower = question.lower()
    if not gold.aggregate_ops and not gold.has_group_by and not gold.has_order_by:
        return True
    detected = set()
    if any(token in lower for token in ["how many", "count", "number of"]):
        detected.add("COUNT")
    if any(token in lower for token in ["average", "avg", "mean"]):
        detected.add("AVG")
    if any(token in lower for token in ["sum", "total", "amount", "arr", "mrr", "pipeline"]):
        detected.add("SUM")
    if any(token in lower for token in ["top", "most", "highest", "least", "lowest"]):
        detected.add("ORDER")
    if " by " in lower or " per " in lower:
        detected.add("GROUP")
    if "rate" in lower or "percentage" in lower:
        detected.add("METRIC")
    if re.search(r"\b(no|not|without)\b", lower):
        detected.add("ANTI_JOIN")
    target = set(gold.aggregate_ops)
    if gold.has_group_by:
        target.add("GROUP")
    if gold.has_order_by and _question_requests_order(question):
        target.add("ORDER")
    if gold.has_expression_metric:
        target.add("METRIC")
    if gold.has_not_exists:
        target.add("ANTI_JOIN")
    if atlas.mode == "semantic" and any(_phrase_matches(question, m) for m in atlas.metric_aliases):
        detected.add("METRIC")
    return bool(detected & target)


def _date_window_hit(question: str, gold: GoldEvidence, atlas: MiniSemanticAtlas) -> bool:
    if not gold.has_date_range:
        return True
    if atlas.mode == "raw":
        return False
    if not _question_has_time_window(question):
        return False
    candidates = atlas.candidate_fields(question)
    if any(_field_role(atlas.columns[field]) == "date" for field in candidates):
        return True
    return any(_field_role(col) == "date" for col in atlas.columns.values())


def _metric_hit(question: str, gold: GoldEvidence, atlas: MiniSemanticAtlas) -> bool:
    if not gold.has_expression_metric:
        return True
    if atlas.mode == "raw":
        return False
    return any(_phrase_matches(question, metric) for metric in atlas.metric_aliases)


def _join_path_hit(gold: GoldEvidence, atlas: MiniSemanticAtlas) -> bool | None:
    if len(gold.tables) < 2:
        return None
    tables = sorted(gold.tables)
    for idx, left in enumerate(tables):
        for right in tables[idx + 1 :]:
            if not atlas.has_join_path(left, right):
                return False
    return True


def _plan_relevant_fields(gold: GoldEvidence, atlas: MiniSemanticAtlas) -> set[str]:
    """Fields a natural-language planner should identify before join rendering.

    Gold SQL includes join-key columns in ON clauses and deterministic ORDER BY
    columns. Those are renderer/planner mechanics, not necessarily NL evidence.
    Excluding ID/FK fields keeps this assessment focused on subject,
    projection, predicate, measure, group, date, and metric fields.
    """
    relevant = {
        field
        for field in gold.columns
        if field in atlas.columns and _field_role(atlas.columns[field]) != "id"
    }
    relevant.update(binding.field for binding in gold.bindings if binding.field in atlas.columns)
    return relevant


def _value_field_recall(bindings: list[GoldBinding], hits: list[AtlasValueHit]) -> float:
    searchable = [
        binding
        for binding in bindings
        if len(_normalize_phrase(binding.value)) >= 2
        and not _is_iso_date(binding.value)
        and not _normalize_phrase(binding.value).isdigit()
    ]
    if not searchable:
        return 1.0
    hit_pairs = {(_normalize_phrase(hit.value), hit.field) for hit in hits}
    matched = 0
    for binding in searchable:
        if (_normalize_phrase(binding.value), binding.field) in hit_pairs:
            matched += 1
    return matched / len(searchable)


def _role_fields_from_question(
    question: str,
    columns: dict[str, AtlasColumn],
) -> set[str]:
    lower = question.lower()
    out: set[str] = set()
    for field, col in columns.items():
        role = _field_role(col)
        name = col.name.lower()
        if role == "display" and any(token in lower for token in ["list", "show", "which", "who"]):
            if any(token in lower for token in ["account", "customer"]) and col.table == "accounts":
                out.add(field)
            if any(token in lower for token in ["rep", "agent", "owner"]) and col.table in {
                "reps",
                "agents",
            }:
                out.add(field)
        if role == "date" and _question_has_time_window(question):
            out.add(field)
        if "owner" in lower and "owner" in name:
            out.add(field)
        if "assignee" in lower or "support rep" in lower:
            if "assignee" in name or col.table in {"agents", "reps"}:
                out.add(field)
    return out


def _field_role(col: AtlasColumn) -> str:
    name = col.name.lower()
    typ = col.sql_type.lower()
    if name in {"id"} or name.endswith("_id") or "code" in name:
        return "id"
    if any(token in name for token in ["date", "on", "created", "opened", "resolved", "renewal"]):
        return "date"
    if any(token in name for token in ["status", "stage", "priority", "severity", "tier", "segment"]):
        return "status"
    if "active" == name or name.startswith("is_") or "breached" in name:
        return "boolean"
    if any(token in typ for token in ["int", "real", "num", "dec", "float"]):
        return "numeric"
    if any(token in name for token in ["name", "domain", "title", "email"]):
        return "display"
    return "attribute"


def _role_aliases(col: AtlasColumn) -> set[str]:
    name = col.name.lower()
    aliases: set[str] = set()
    if _field_role(col) == "date":
        aliases.update({"date", "time", "when"})
        if "signup" in name or "created" in name:
            aliases.update({"signed up", "signup", "created"})
        if "renewal" in name:
            aliases.update({"renewal", "renews"})
        if "resolved" in name:
            aliases.update({"resolved", "resolution date"})
        if "issued" in name:
            aliases.update({"issued", "invoice date"})
    if _field_role(col) == "display":
        aliases.update({"name", "display name"})
    if name in {"owner_rep_id", "owner_agent_id"}:
        aliases.update({"owner", "owned by", "account owner", "customer owner"})
    if name in {"assignee_rep_id", "assignee_id"}:
        aliases.update({"assignee", "support rep", "support agent", "resolved by"})
    if name == "arr":
        aliases.update({"arr", "annual recurring revenue", "recurring revenue"})
    if name == "mrr":
        aliases.update({"mrr", "monthly recurring revenue"})
    if name == "amount":
        aliases.update({"amount", "invoice amount", "pipeline amount", "paid amount"})
    if name == "resolution_hours":
        aliases.update({"resolution hours", "resolution time"})
    if name == "health_score":
        aliases.update({"health score", "health"})
    if name == "source_channel":
        aliases.update({"source", "channel", "lead source"})
    if name == "source_campaign_id":
        aliases.update({"campaign", "marketing campaign"})
    if name == "event_type":
        aliases.update({"event", "event type", "login", "cancellation"})
    if name == "sla_breached":
        aliases.update({"sla breach", "breach", "breached"})
    return aliases


def _sample_value_mentions_question(qnorm: str, value: str) -> bool:
    vnorm = _normalize_phrase(value)
    if len(vnorm) < 2:
        return False
    if vnorm.isdigit() and len(vnorm) < 3:
        return False
    return re.search(rf"(^|\b){re.escape(vnorm)}($|\b)", qnorm) is not None


def _literal_compatible_with_field(
    question: str,
    literal: str,
    col: AtlasColumn,
    aliases: set[str],
) -> bool:
    role = _field_role(col)
    if _is_iso_date(literal) or _is_year(literal):
        return role == "date"
    if re.fullmatch(r"\d+(?:\.\d+)?", literal):
        if role != "numeric":
            return False
        context = _mention_context(question, literal)
        return _any_phrase_matches(context, aliases)
    return False


def _structured_literals(question: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for match in re.finditer(r"\b\d{4}-\d{1,2}-\d{1,2}\b|\b(?:19|20)\d{2}\b|\b\d+(?:\.\d+)?\b", question):
        value = match.group(0)
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _should_sample(col: AtlasColumn) -> bool:
    role = _field_role(col)
    if role in {"display", "status", "boolean", "id", "date"}:
        return True
    return role == "numeric" and any(token in col.name.lower() for token in ["score", "amount"])


def _add_inferred_edges(
    table_columns: dict[str, list[AtlasColumn]],
    graph: dict[str, set[str]],
) -> None:
    table_names = set(table_columns)
    singular_tables = {_singular(table): table for table in table_names}
    for table, columns in table_columns.items():
        for col in columns:
            name = col.name.lower()
            if not name.endswith("_id"):
                continue
            stem = name.removesuffix("_id")
            target = singular_tables.get(stem) or singular_tables.get(stem.removesuffix("_rep"))
            if target and target != table:
                graph.setdefault(table, set()).add(target)
                graph.setdefault(target, set()).add(table)


def _first_column(node: exp.Expression | None) -> exp.Column | None:
    if node is None:
        return None
    if isinstance(node, exp.Column):
        return node
    return next(node.find_all(exp.Column), None)


def _first_literal(node: exp.Expression | None) -> exp.Literal | None:
    if node is None:
        return None
    if isinstance(node, exp.Literal):
        return node
    return next(node.find_all(exp.Literal), None)


def _dedupe_bindings(bindings: list[GoldBinding]) -> list[GoldBinding]:
    seen: set[tuple[str, str, str]] = set()
    out: list[GoldBinding] = []
    for binding in bindings:
        key = (binding.field, _normalize_phrase(binding.value), binding.operator)
        if key not in seen:
            seen.add(key)
            out.append(binding)
    return out


def _dedupe_value_hits(hits: list[AtlasValueHit]) -> list[AtlasValueHit]:
    seen: set[tuple[str, str, str]] = set()
    out: list[AtlasValueHit] = []
    for hit in hits:
        key = (hit.field, _normalize_phrase(hit.value), _normalize_phrase(hit.mention))
        if key not in seen:
            seen.add(key)
            out.append(hit)
    return out


def _has_semantic_tables(atlas: MiniSemanticAtlas, names: set[str]) -> bool:
    return bool(atlas.tables & names)


def _any_phrase_matches(text: str, phrases: set[str]) -> bool:
    return any(_phrase_matches(text, phrase) for phrase in phrases)


def _phrase_matches(text: str, phrase: str) -> bool:
    normalized = _normalize_phrase(text)
    target = _normalize_phrase(phrase)
    if len(target) < 2:
        return False
    return re.search(rf"(^|\b){re.escape(target)}($|\b)", normalized) is not None


def _normalize_phrase(value: str) -> str:
    value = re.sub(r"[_/-]+", " ", value.lower())
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _tokens(value: str) -> set[str]:
    return {_singular(token) for token in re.findall(r"[a-z0-9]+", value.lower())}


def _tokens_to_phrases(value: str) -> set[str]:
    tokens = [token for token in _tokens(value) if len(token) > 2 and token not in _STOP_WORDS]
    return set(tokens)


def _name_phrases(value: str) -> set[str]:
    normalized = _normalize_phrase(value)
    tokens = normalized.split()
    out = {normalized, *tokens}
    out.add(_singular(normalized))
    return {item for item in out if item and item not in _STOP_WORDS}


def _singular(value: str) -> str:
    if len(value) > 3 and value.endswith("ies"):
        return value[:-3] + "y"
    if len(value) > 3 and value.endswith("s") and not value.endswith(("ss", "us")):
        return value[:-1]
    return value


def _is_iso_date(value: str) -> bool:
    return bool(re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", value.strip()))


def _is_year(value: str) -> bool:
    return bool(re.fullmatch(r"(?:19|20)\d{2}", value.strip()))


def _question_has_time_window(question: str) -> bool:
    lower = question.lower()
    months = (
        "january",
        "february",
        "march",
        "april",
        "may",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
    )
    return any(month in lower for month in months) or bool(
        re.search(r"\b(?:yesterday|today|last week|last month|q[1-4]|20\d{2})\b", lower)
    )


def _question_requests_order(question: str) -> bool:
    lower = question.lower()
    return any(
        token in lower
        for token in [
            "top",
            "most",
            "highest",
            "lowest",
            "least",
            "sort",
            "order",
            "rank",
            "earliest",
            "latest",
        ]
    )


def _mention_context(question: str, mention: str, radius: int = 36) -> str:
    lower = question.lower()
    idx = lower.find(mention.lower())
    if idx < 0:
        return question
    return question[max(0, idx - radius) : min(len(question), idx + len(mention) + radius)]


def _recall(gold: set[str], predicted: set[str]) -> float:
    if not gold:
        return 1.0
    return len(gold & predicted) / len(gold)


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _avg_bool(values: list[bool]) -> float:
    return sum(1 for value in values if value) / len(values) if values else 0.0


def _case_bucket(case: dict[str, Any], mode: AtlasMode) -> str:
    row = case["modes"][mode]
    if case["disposition"] != "route":
        return "fail_closed" if row["fail_closed"] else "wrong_accept_risk"
    return "plan_ready" if row["plan_ready"] else "not_ready"


_STOP_WORDS = {
    "a",
    "all",
    "and",
    "by",
    "for",
    "from",
    "in",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
}

_TABLE_SYNONYMS: dict[str, set[str]] = {
    "accounts": {"account", "accounts", "customer", "customers", "company", "companies"},
    "agents": {"agent", "agents", "owner", "owners", "support agent", "support rep"},
    "reps": {"rep", "reps", "owner", "owners", "sales rep", "support rep", "success rep"},
    "regions": {"region", "regions", "geography", "territory"},
    "plans": {"plan", "plans", "tier", "subscription plan"},
    "invoices": {"invoice", "invoices", "billing", "paid invoice", "overdue invoice"},
    "tickets": {"ticket", "tickets", "support", "case", "cases"},
    "events": {"event", "events", "login", "cancellation", "upgrade"},
    "usage_events": {"usage", "event", "events", "login", "activation"},
    "opportunities": {"opportunity", "opportunities", "pipeline", "deal", "deals"},
    "campaigns": {"campaign", "campaigns", "marketing campaign"},
    "leads": {"lead", "leads", "prospect", "funnel"},
    "subscriptions": {"subscription", "subscriptions", "mrr", "recurring revenue"},
    "activities": {"activity", "activities", "sales activity", "touchpoint"},
    "nps_responses": {"nps", "survey", "response", "responses"},
}

_FIELD_SYNONYMS: dict[str, set[str]] = {
    "company_name": {"company", "company name", "account", "customer", "customer name"},
    "full_name": {"name", "full name", "owner", "rep", "agent", "support rep", "sales rep"},
    "signup_date": {"signup date", "signed up", "created"},
    "created_on": {"created", "created on", "signup", "signed up"},
    "issued_on": {"issued", "invoice date", "billed"},
    "paid_on": {"paid date", "paid on"},
    "opened_on": {"opened", "opened on"},
    "resolved_on": {"resolved", "resolved on"},
    "close_date": {"closed", "close date"},
    "renewal_date": {"renewal", "renews", "renewal date"},
    "source_channel": {"source", "channel", "lead source"},
    "source_campaign_id": {"campaign", "source campaign", "marketing campaign"},
    "owner_rep_id": {"owner", "account owner", "customer owner"},
    "owner_agent_id": {"owner", "account owner", "customer owner"},
    "assignee_id": {"assignee", "support agent", "support rep"},
    "assignee_rep_id": {"assignee", "support agent", "support rep"},
    "arr": {"arr", "annual recurring revenue"},
    "mrr": {"mrr", "monthly recurring revenue"},
    "amount": {"amount", "paid amount", "invoice amount", "pipeline amount"},
    "resolution_hours": {"resolution hours", "resolution time"},
    "sla_breached": {"sla breach", "sla breached", "breach rate"},
    "health_score": {"health", "health score"},
}
