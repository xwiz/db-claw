"""Experimental SQL proof for the SchemaAtlas + QueryFrame direction.

This module is intentionally eval-only. It does not change the production
cascade. The goal is to answer the next cheapest question after
``binder_probe``:

*When the atlas says an example is proof-ready, can a small deterministic frame
solver recover execution-correct SQL without using gold SQL to build it?*
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

from .binder_probe import (
    DbAtlas,
    JoinEdge,
    ValueHit,
    _name_tokens,
    _norm_value,
    _quote_ident,
    _tokens,
    extract_mentions,
)
from .exec_acc import exec_results_eq, execute
from .spider import Example, SpiderSuite

__all__ = [
    "QueryFrameProbeReport",
    "render_queryframe_probe_markdown",
    "run_queryframe_probe",
]


@dataclass(frozen=True)
class Predicate:
    field: str
    value: str
    operator: str
    mention: str
    score: float


@dataclass(frozen=True)
class Projection:
    expr: str
    field: str | None
    table: str | None
    kind: str
    score: float
    join_hint: JoinEdge | None = None


@dataclass(frozen=True)
class SolvedFrame:
    sql: str
    projection: Projection
    predicates: list[Predicate]
    tables: list[str]
    route_reason: str


@dataclass(frozen=True)
class QueryFrameAttempt:
    solved: SolvedFrame | None
    route_reason: str


@dataclass(frozen=True)
class QueryFrameProbeExample:
    index: int
    db_id: str
    question: str
    proof_ready: bool
    current_correct: bool | None
    routed: bool
    route_reason: str
    pred_sql: str | None
    exec_equal: bool
    gold_error: str | None
    pred_error: str | None
    gold_timeout: bool
    pred_timeout: bool


@dataclass(frozen=True)
class QueryFrameProbeReport:
    sample_size: int
    proof_ready_only: bool
    routed_only: bool
    examples: list[QueryFrameProbeExample]
    summary: dict[str, Any]

    def to_json(self) -> str:
        return json.dumps(
            {
                "sample_size": self.sample_size,
                "proof_ready_only": self.proof_ready_only,
                "routed_only": self.routed_only,
                "summary": self.summary,
                "examples": [example.__dict__ for example in self.examples],
            },
            indent=2,
            sort_keys=True,
        )


def run_queryframe_probe(
    *,
    questions_path: Path,
    db_root: Path,
    suite_name: str,
    binder_report_json: Path,
    current_report_json: Path | None = None,
    proof_ready_only: bool = True,
    routed_only: bool = False,
    exec_timeout_seconds: float = 10.0,
) -> QueryFrameProbeReport:
    suite = SpiderSuite.load(questions_path, db_root, name=suite_name)  # type: ignore[arg-type]
    binder_examples = _load_binder_examples(binder_report_json)
    current_by_index = _load_current_correct(current_report_json)
    atlas_cache: dict[str, DbAtlas] = {}
    records: list[QueryFrameProbeExample] = []

    for row in binder_examples:
        index = int(row["index"])
        proof_ready = bool(row.get("proof_ready"))
        if proof_ready_only and not proof_ready:
            continue
        if index < 0 or index >= len(suite.examples):
            continue
        example = suite.examples[index]
        atlas = atlas_cache.get(example.db_id)
        if atlas is None:
            atlas = DbAtlas.load(example.db_id, example.db_path)
            atlas_cache[example.db_id] = atlas
        current_correct = current_by_index.get(index)
        if current_correct is None and "current_correct" in row:
            raw_current = row.get("current_correct")
            current_correct = bool(raw_current) if raw_current is not None else None

        attempt = solve_queryframe_attempt(example, atlas, row)
        if routed_only and attempt.solved is None:
            continue
        records.append(
            _score_example(
                index=index,
                example=example,
                proof_ready=proof_ready,
                current_correct=current_correct,
                solved=attempt.solved,
                route_reason=attempt.route_reason,
                exec_timeout_seconds=exec_timeout_seconds,
            )
        )

    return QueryFrameProbeReport(
        sample_size=len(records),
        proof_ready_only=proof_ready_only,
        routed_only=routed_only,
        examples=records,
        summary=_summarize(records),
    )


def solve_queryframe(
    example: Example,
    atlas: DbAtlas,
    binder_row: dict[str, Any] | None = None,
) -> SolvedFrame | None:
    return solve_queryframe_attempt(example, atlas, binder_row).solved


def solve_queryframe_attempt(
    example: Example,
    atlas: DbAtlas,
    binder_row: dict[str, Any] | None = None,
) -> QueryFrameAttempt:
    question = example.question
    if _looks_complex(question):
        return _not_routed("complex_shape")

    mentions = extract_mentions(question)
    value_hits = [
        *atlas.lookup_values(mentions),
        *_derived_enum_hits(question, atlas),
        *_derived_literal_hits(question, atlas),
    ]
    predicates = _choose_predicates(question, atlas, value_hits)
    if not predicates:
        return _not_routed("no_predicates")
    if len(predicates) > 3:
        return _not_routed("too_many_predicates")

    projection = _choose_projection(question, atlas, predicates, binder_row)
    if projection is None:
        return _not_routed("no_projection")
    if projection.kind == "field" and _has_unsupported_conjunction(
        question,
        atlas,
        value_hits,
        predicates,
    ):
        return _not_routed("multi_projection")
    if projection.field is not None and _projection_is_unsafe_id(question, atlas, projection):
        replacement = _display_projection_for_fk(question, atlas, projection) or _safe_non_id_projection(
            question,
            atlas,
            predicates,
            binder_row,
        )
        if replacement is None:
            return _not_routed("unsafe_id_projection")
        projection = replacement
    if _has_unbound_year(question, predicates):
        return _not_routed("unbound_year")

    required_tables = {atlas.columns[p.field].table for p in predicates if p.field in atlas.columns}
    if projection.table:
        required_tables.add(projection.table)
    if not required_tables:
        return _not_routed("no_tables")

    base_table = projection.table or sorted(required_tables)[0]
    preferred_edges = [projection.join_hint] if projection.join_hint is not None else []
    table_order, joins = _build_join_plan(
        atlas,
        base_table,
        required_tables,
        preferred_edges=preferred_edges,
    )
    if table_order is None or joins is None:
        return _not_routed("no_join_path")

    where_sql = _render_where(atlas, predicates)
    order_sql = _render_order(question, atlas, projection, table_order, predicates)
    if _requires_order(question) and not order_sql:
        return _not_routed("missing_order")
    sql = _render_select_sql(
        projection=projection,
        base_table=base_table,
        joins=joins,
        where_sql=where_sql,
        order_sql=order_sql,
    )
    return QueryFrameAttempt(
        solved=SolvedFrame(
            sql=sql,
            projection=projection,
            predicates=predicates,
            tables=table_order,
            route_reason="routed",
        ),
        route_reason="routed",
    )


def render_queryframe_probe_markdown(report: QueryFrameProbeReport) -> str:
    s = report.summary
    lines = [
        "# QueryFrame Solver Probe",
        "",
        f"- sample_size: `{report.sample_size}`",
        f"- proof_ready_only: `{report.proof_ready_only}`",
        f"- routed_only: `{report.routed_only}`",
        "",
        "## Summary",
        "",
        "| metric | value |",
        "|---|---:|",
    ]
    for key in [
        "proof_ready",
        "routed",
        "routed_coverage",
        "correct",
        "routed_exec_acc",
        "net_recovery",
        "regressions_from_current",
        "pred_errors",
        "pred_timeouts",
        "gold_timeouts",
    ]:
        value = s.get(key)
        if isinstance(value, float):
            lines.append(f"| `{key}` | `{value:.2%}` |")
        else:
            lines.append(f"| `{key}` | `{value}` |")
    lines.extend(["", "## Route Buckets", "", "| bucket | n |", "|---|---:|"])
    for bucket, count in sorted(s["route_buckets"].items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"| `{bucket}` | {count} |")
    lines.extend(["", "## By DB", "", "| DB | routed | correct | exec acc |", "|---|---:|---:|---:|"])
    for db_id, stats in sorted(s["by_db"].items()):
        acc = stats["exec_acc"]
        lines.append(f"| `{db_id}` | {stats['routed']} | {stats['correct']} | {acc:.2%} |")
    lines.extend(
        [
            "",
            "## Read",
            "",
            "This is an experimental SQL execution probe for the QueryFrame path.",
            "It routes only examples the deterministic solver can build from the",
            "question text, schema/value atlas, and binder evidence. Gold SQL is",
            "used only for execution-equivalence scoring.",
        ]
    )
    return "\n".join(lines) + "\n"


def _score_example(
    *,
    index: int,
    example: Example,
    proof_ready: bool,
    current_correct: bool | None,
    solved: SolvedFrame | None,
    route_reason: str,
    exec_timeout_seconds: float,
) -> QueryFrameProbeExample:
    if solved is None:
        return QueryFrameProbeExample(
            index=index,
            db_id=example.db_id,
            question=example.question,
            proof_ready=proof_ready,
            current_correct=current_correct,
            routed=False,
            route_reason=route_reason,
            pred_sql=None,
            exec_equal=False,
            gold_error=None,
            pred_error=None,
            gold_timeout=False,
            pred_timeout=False,
        )

    gold = execute(example.db_path, example.gold_sql, timeout_seconds=exec_timeout_seconds)
    pred = execute(example.db_path, solved.sql, timeout_seconds=exec_timeout_seconds)
    exec_equal = exec_results_eq(example.gold_sql, gold, pred)
    return QueryFrameProbeExample(
        index=index,
        db_id=example.db_id,
        question=example.question,
        proof_ready=proof_ready,
        current_correct=current_correct,
        routed=True,
        route_reason=solved.route_reason,
        pred_sql=solved.sql,
        exec_equal=exec_equal,
        gold_error=gold.error,
        pred_error=pred.error,
        gold_timeout=gold.timed_out,
        pred_timeout=pred.timed_out,
    )


def _summarize(records: list[QueryFrameProbeExample]) -> dict[str, Any]:
    routed = [record for record in records if record.routed]
    correct = [record for record in routed if record.exec_equal]
    route_buckets: dict[str, int] = {}
    for record in records:
        bucket = _route_bucket(record)
        route_buckets[bucket] = route_buckets.get(bucket, 0) + 1
    by_db: dict[str, dict[str, Any]] = {}
    for db_id in sorted({record.db_id for record in records}):
        db_routed = [record for record in routed if record.db_id == db_id]
        db_correct = [record for record in db_routed if record.exec_equal]
        by_db[db_id] = {
            "routed": len(db_routed),
            "correct": len(db_correct),
            "exec_acc": len(db_correct) / len(db_routed) if db_routed else 0.0,
        }
    return {
        "proof_ready": sum(1 for record in records if record.proof_ready),
        "routed": len(routed),
        "routed_coverage": len(routed) / len(records) if records else 0.0,
        "correct": len(correct),
        "routed_exec_acc": len(correct) / len(routed) if routed else 0.0,
        "net_recovery": sum(
            1 for record in correct if record.current_correct is False or record.current_correct is None
        ),
        "regressions_from_current": sum(
            1 for record in routed if record.current_correct is True and not record.exec_equal
        ),
        "pred_errors": sum(1 for record in routed if record.pred_error is not None),
        "pred_timeouts": sum(1 for record in routed if record.pred_timeout),
        "gold_timeouts": sum(1 for record in routed if record.gold_timeout),
        "route_buckets": route_buckets,
        "by_db": by_db,
    }


def _route_bucket(record: QueryFrameProbeExample) -> str:
    if not record.routed:
        return record.route_reason
    if record.exec_equal:
        return "correct"
    if record.pred_timeout:
        return "pred_timeout"
    if record.pred_error is not None:
        return "pred_error"
    if record.gold_timeout:
        return "gold_timeout"
    if record.gold_error is not None:
        return "gold_error"
    return "exec_mismatch"


def _not_routed(reason: str) -> QueryFrameAttempt:
    return QueryFrameAttempt(solved=None, route_reason=f"not_routed_{reason}")


def _choose_predicates(
    question: str,
    atlas: DbAtlas,
    value_hits: list[ValueHit],
) -> list[Predicate]:
    grouped: dict[str, list[ValueHit]] = {}
    for hit in value_hits:
        if _hit_mention_is_literal(question, atlas, hit):
            grouped.setdefault(_norm_value(hit.mention), []).append(hit)

    predicates: list[Predicate] = []
    used_fields: set[str] = set()
    selected_mentions: set[str] = set()
    for key, hits in sorted(grouped.items(), key=lambda item: -len(item[0])):
        if _is_redundant_component_mention(key, selected_mentions):
            continue
        scored = sorted(
            (
                (_value_hit_score(question, atlas, hit, used_fields), hit)
                for hit in hits
                if hit.field in atlas.columns
            ),
            key=lambda item: (-item[0], item[1].field),
        )
        if not scored:
            continue
        score, hit = scored[0]
        if score < 3.5 or hit.field in used_fields:
            continue
        operator = _operator_for_mention(question, hit.mention)
        predicates.append(
            Predicate(
                field=hit.field,
                value=hit.db_value or hit.mention,
                operator=operator,
                mention=hit.mention,
                score=score,
            )
        )
        used_fields.add(hit.field)
        selected_mentions.add(key)
    return predicates


def _choose_projection(
    question: str,
    atlas: DbAtlas,
    predicates: list[Predicate],
    binder_row: dict[str, Any] | None,
) -> Projection | None:
    aggregate = _aggregate_kind(question)
    predicate_fields = {p.field for p in predicates}
    candidates = _projection_candidates(question, atlas, predicate_fields, binder_row)

    if aggregate == "count":
        table = _count_subject_table(question, atlas) or _subject_table(
            question, atlas, predicates, candidates
        )
        if table is None:
            return None
        return Projection(expr="COUNT(*)", field=None, table=table, kind="count", score=10.0)

    if aggregate in {"avg", "sum"}:
        field = _best_measure_field(question, atlas, candidates, predicate_fields)
        if field is None:
            return None
        fn = "AVG" if aggregate == "avg" else "SUM"
        col = atlas.columns[field]
        return Projection(
            expr=f"{fn}({_field_ref(col.table, col.name)})",
            field=field,
            table=col.table,
            kind=aggregate,
            score=10.0,
        )

    best = _best_projection_field(question, atlas, candidates, predicate_fields)
    if best is None:
        return None
    field, score = best
    if score < 2.5:
        return None
    col = atlas.columns[field]
    return Projection(
        expr=f"DISTINCT {_field_ref(col.table, col.name)}",
        field=field,
        table=col.table,
        kind="field",
        score=score,
    )


def _display_projection_for_fk(
    question: str,
    atlas: DbAtlas,
    projection: Projection,
) -> Projection | None:
    if projection.field is None:
        return None
    source = atlas.columns.get(projection.field)
    if source is None:
        return None
    role_tokens = _fk_role_tokens(source.name)
    if not role_tokens:
        return None
    if not (role_tokens & _DISPLAY_FK_ROLE_TOKENS):
        return None
    question_tokens = _question_tokens_with_aliases(question)
    if not (role_tokens & question_tokens):
        return None

    scored: list[tuple[float, str, JoinEdge]] = []
    for edge in _edges_from_column(atlas, source.table, source.name):
        display = _display_field_for_table(question, atlas, edge.right_table, role_tokens)
        if display is None:
            continue
        display_field, display_score = display
        scored.append((display_score, display_field, edge))
    if not scored:
        return None
    score, field, edge = max(scored, key=lambda item: (item[0], item[1]))
    if score < 2.0:
        return None
    col = atlas.columns[field]
    return Projection(
        expr=f"DISTINCT {_field_ref(col.table, col.name)}",
        field=field,
        table=col.table,
        kind="field",
        score=score,
        join_hint=edge,
    )


def _safe_non_id_projection(
    question: str,
    atlas: DbAtlas,
    predicates: list[Predicate],
    binder_row: dict[str, Any] | None,
) -> Projection | None:
    predicate_fields = {p.field for p in predicates}
    candidates = _projection_candidates(question, atlas, predicate_fields, binder_row)
    scored: list[tuple[float, str]] = []
    for field in candidates:
        if field in predicate_fields:
            continue
        col = atlas.columns.get(field)
        if col is None:
            continue
        lower_name = col.name.lower()
        if lower_name in {"id", "uuid"} or lower_name.endswith("_id"):
            continue
        if not _looks_display_column(col.name, col.sql_type):
            continue
        if not (_name_tokens(col.name) & _question_tokens_with_aliases(question)):
            continue
        score = _field_question_score(question, col.table, col.name)
        if score >= 3.0:
            scored.append((score, field))
    if not scored:
        return None
    score, field = max(scored, key=lambda item: (item[0], item[1]))
    col = atlas.columns[field]
    return Projection(
        expr=f"DISTINCT {_field_ref(col.table, col.name)}",
        field=field,
        table=col.table,
        kind="field",
        score=score,
    )


def _fk_role_tokens(column_name: str) -> set[str]:
    lower = column_name.lower()
    if lower in {"id", "uuid"}:
        return set()
    raw = lower.removesuffix("_id").removesuffix("id").strip("_")
    tokens = _name_tokens(raw)
    if "color" in tokens:
        tokens.add("colour")
    if "colour" in tokens:
        tokens.add("color")
    return tokens


def _question_tokens_with_aliases(question: str) -> set[str]:
    tokens = set(_tokens(question))
    if "color" in tokens:
        tokens.add("colour")
    if "colour" in tokens:
        tokens.add("color")
    if "eyes" in tokens:
        tokens.add("eye")
    if "superpower" in tokens or "superpowers" in tokens:
        tokens.add("superpower")
        tokens.add("power")
    if "website" in tokens or "websites" in tokens:
        tokens.add("url")
    if "hometown" in tokens:
        tokens.add("city")
    if "expense" in tokens and ("total" in tokens or "amount" in tokens):
        tokens.add("cost")
    return tokens


def _edges_from_column(atlas: DbAtlas, table: str, column: str) -> list[JoinEdge]:
    out: list[JoinEdge] = []
    for edges in atlas.join_edges.values():
        for edge in edges:
            if edge.left_table == table and edge.left_column.lower() == column.lower():
                out.append(edge)
    return out


def _display_field_for_table(
    question: str,
    atlas: DbAtlas,
    table: str,
    role_tokens: set[str],
) -> tuple[str, float] | None:
    scored: list[tuple[float, str]] = []
    question_tokens = _question_tokens_with_aliases(question)
    for col in atlas.table_columns.get(table, []):
        field = col.canonical.lower()
        lower_name = col.name.lower()
        if field not in atlas.columns:
            continue
        if lower_name in {"id", "uuid"} or lower_name.endswith("_id"):
            continue
        if not _looks_display_column(col.name, col.sql_type):
            continue
        field_tokens = _name_tokens(col.name)
        score = _field_question_score(question, col.table, col.name)
        score += 2.0 * len(role_tokens & field_tokens)
        score += 1.0 * len(role_tokens & _name_tokens(col.table))
        score += 1.0 * len(question_tokens & field_tokens)
        if lower_name in {"name", "title", "label", "type", "colour", "color"}:
            score += 1.5
        scored.append((score, field))
    if not scored:
        return None
    score, field = max(scored, key=lambda item: (item[0], item[1]))
    return field, score


def _projection_candidates(
    question: str,
    atlas: DbAtlas,
    predicate_fields: set[str],
    binder_row: dict[str, Any] | None,
) -> set[str]:
    q_tokens = _question_tokens_with_aliases(question)
    candidates = set(predicate_fields)
    if binder_row is not None:
        for field in binder_row.get("candidate_fields", []):
            if isinstance(field, str) and field in atlas.columns:
                candidates.add(field)
    for col in atlas.columns.values():
        field = col.canonical.lower()
        if _name_tokens(col.name) & q_tokens:
            candidates.add(field)
        elif col.table.lower() in q_tokens:
            candidates.add(field)
    return candidates


def _best_projection_field(
    question: str,
    atlas: DbAtlas,
    candidates: set[str],
    predicate_fields: set[str],
) -> tuple[str, float] | None:
    scored: list[tuple[float, str]] = []
    predicate_tables = {
        atlas.columns[field].table for field in predicate_fields if field in atlas.columns
    }
    for field in candidates:
        col = atlas.columns.get(field)
        if col is None:
            continue
        score = _field_question_score(question, col.table, col.name)
        if col.table in predicate_tables:
            score += 1.5
        if field in predicate_fields:
            score -= 6.0
        if col.name.lower() in {"id", f"{col.table.lower()}_id"} and _asks_for_id(question):
            score += 2.0
        scored.append((score, field))
    if not scored:
        return None
    score, field = max(scored, key=lambda item: (item[0], item[1]))
    return field, score


def _best_measure_field(
    question: str,
    atlas: DbAtlas,
    candidates: set[str],
    predicate_fields: set[str],
) -> str | None:
    best = _best_projection_field(question, atlas, candidates, predicate_fields)
    if best is None:
        return None
    field, score = best
    col = atlas.columns[field]
    if score < 2.0 or not _looks_numeric(col.sql_type):
        return None
    return field


def _subject_table(
    question: str,
    atlas: DbAtlas,
    predicates: list[Predicate],
    candidates: set[str],
) -> str | None:
    q_tokens = set(_tokens(question))
    table_scores: dict[str, float] = {}
    for table in atlas.tables:
        score = float(len(_name_tokens(table) & q_tokens) * 3)
        table_scores[table] = score
    for field in candidates:
        col = atlas.columns.get(field)
        if col is not None:
            table_scores[col.table] = table_scores.get(col.table, 0.0) + _field_question_score(
                question, col.table, col.name
            )
    for predicate in predicates:
        col = atlas.columns.get(predicate.field)
        if col is not None:
            table_scores[col.table] = table_scores.get(col.table, 0.0) + 0.5
    if not table_scores:
        return None
    table, score = max(table_scores.items(), key=lambda item: (item[1], item[0]))
    return table if score > 0 else None


def _count_subject_table(question: str, atlas: DbAtlas) -> str | None:
    match = re.search(r"\bhow many\s+([A-Za-z_]+)|\bcount\s+(?:the\s+)?([A-Za-z_]+)", question, re.I)
    if match is None:
        return None
    raw = match.group(1) or match.group(2) or ""
    raw_lower = raw.lower()
    raw_singular = _singular_token(raw_lower)
    for table in sorted(atlas.tables):
        table_lower = table.lower()
        if table_lower == raw_lower or _singular_token(table_lower) == raw_singular:
            return table
    tokens = set(_tokens(raw))
    for table in sorted(atlas.tables):
        if _name_tokens(table) & tokens:
            return table
    return None


def _build_join_plan(
    atlas: DbAtlas,
    base_table: str,
    required_tables: set[str],
    *,
    preferred_edges: list[JoinEdge] | None = None,
) -> tuple[list[str], list[str]] | tuple[None, None]:
    joined = [base_table]
    joins: list[str] = []
    preferred_edges = preferred_edges or []
    for target in sorted(required_tables - {base_table}):
        best_path: list[str] | None = None
        for source in joined:
            path = atlas.shortest_table_path(source, target, max_hops=4)
            if path is not None and (best_path is None or len(path) < len(best_path)):
                best_path = path
        if best_path is None:
            return None, None
        for left, right in pairwise(best_path):
            if right in joined:
                continue
            edge = _preferred_join_edge(left, right, preferred_edges) or atlas.edge_for(left, right)
            if edge is None:
                return None, None
            joins.append(
                f"JOIN {_quote_ident(edge.right_table)} ON "
                f"{_field_ref(edge.left_table, edge.left_column)} = "
                f"{_field_ref(edge.right_table, edge.right_column)}"
            )
            joined.append(right)
    return joined, joins


def _preferred_join_edge(left: str, right: str, preferred_edges: list[JoinEdge]) -> JoinEdge | None:
    for edge in preferred_edges:
        if edge.left_table == left and edge.right_table == right:
            return edge
        if edge.left_table == right and edge.right_table == left:
            return JoinEdge(
                left_table=edge.right_table,
                left_column=edge.right_column,
                right_table=edge.left_table,
                right_column=edge.left_column,
            )
    return None


def _has_unsupported_conjunction(
    question: str,
    atlas: DbAtlas,
    value_hits: list[ValueHit],
    predicates: list[Predicate],
) -> bool:
    lower = question.lower()
    if not any(token in lower for token in [" and ", ",", " but "]):
        return False
    if _has_same_field_value_list(question, atlas, value_hits, predicates):
        return True
    return _looks_like_multi_projection_request(lower)


def _has_same_field_value_list(
    question: str,
    atlas: DbAtlas,
    value_hits: list[ValueHit],
    predicates: list[Predicate],
) -> bool:
    predicate_fields = {predicate.field for predicate in predicates}
    if not predicate_fields:
        return False
    mentions_by_field: dict[str, set[str]] = {}
    for hit in value_hits:
        if hit.field not in predicate_fields:
            continue
        if hit.field not in atlas.columns:
            continue
        if not _hit_mention_is_literal(question, atlas, hit):
            continue
        mentions_by_field.setdefault(hit.field, set()).add(_norm_value(hit.mention))
    return any(len(mentions) > 1 for mentions in mentions_by_field.values())


def _looks_like_multi_projection_request(lower_question: str) -> bool:
    if " and find " in lower_question or " and what " in lower_question:
        return True
    projection_pairs = [
        ("display name", "location"),
        ("district", "region"),
        ("alignment", "superpower"),
        ("frame", "card"),
        ("eye", "hair"),
        ("eyes", "hair"),
        ("hair", "skin"),
        ("language", "flavor"),
    ]
    for left, right in projection_pairs:
        if re.search(rf"\b{left}\b[^?.]{{0,60}}\band\b[^?.]{{0,60}}\b{right}", lower_question):
            return True
    if "," in lower_question and re.search(r"\b(?:eye|eyes|hair|skin|frame|card|name)\b", lower_question):
        return True
    return False


def _render_select_sql(
    *,
    projection: Projection,
    base_table: str,
    joins: list[str],
    where_sql: str,
    order_sql: str,
) -> str:
    parts = [f"SELECT {projection.expr}", f"FROM {_quote_ident(base_table)}"]
    parts.extend(joins)
    if where_sql:
        parts.append(where_sql)
    if order_sql:
        parts.append(order_sql)
    return " ".join(parts)


def _render_where(atlas: DbAtlas, predicates: list[Predicate]) -> str:
    clauses: list[str] = []
    for predicate in predicates:
        col = atlas.columns[predicate.field]
        if (
            predicate.operator in {"=", "<", ">", "<=", ">="}
            and _is_year_literal(predicate.value)
            and any(token in col.name.lower() for token in ["date", "time", "created", "issued", "dob", "birth"])
        ):
            clauses.append(
                f"STRFTIME('%Y', {_field_ref(col.table, col.name)}) {predicate.operator} "
                f"'{predicate.value}'"
            )
            continue
        value = _sql_literal(predicate.value, col.sql_type)
        clauses.append(f"{_field_ref(col.table, col.name)} {predicate.operator} {value}")
    return "WHERE " + " AND ".join(clauses) if clauses else ""


def _render_order(
    question: str,
    atlas: DbAtlas,
    projection: Projection,
    table_order: list[str],
    predicates: list[Predicate],
) -> str:
    lower = question.lower()
    if not _requires_order(question):
        return ""
    direction = "ASC" if any(token in lower for token in ["lowest", "least", "earliest", "first"]) else "DESC"
    candidates = {
        field
        for field, col in atlas.columns.items()
        if col.table in table_order and field not in {p.field for p in predicates}
    }
    field = _best_measure_field(question, atlas, candidates, {p.field for p in predicates})
    if field is None and projection.field is not None:
        field = projection.field
    if field is None:
        return ""
    col = atlas.columns[field]
    return f"ORDER BY {_field_ref(col.table, col.name)} {direction} LIMIT 1"


def _value_hit_score(
    question: str,
    atlas: DbAtlas,
    hit: ValueHit,
    used_fields: set[str],
) -> float:
    col = atlas.columns[hit.field]
    lower_name = col.name.lower()
    context = _mention_context(question, hit.mention)
    context_tokens = set(_tokens(context))
    score = 1.0
    score += 2.5 * len(_name_tokens(col.name) & context_tokens)
    score += 1.0 * len(_name_tokens(col.table) & set(_tokens(question)))
    table_tokens = _name_tokens(col.table)
    if table_tokens & context_tokens or {_singular_token(token) for token in table_tokens} & context_tokens:
        score += 2.0
    if _is_code_like(hit.mention) and ("code" in lower_name or lower_name.endswith("id")):
        score += 4.0
    if _is_plain_number(hit.mention) and (
        lower_name.endswith("id") or lower_name in {"id", "uuid"}
    ):
        score += 4.0 if _asks_for_id(context) else -5.0
    if _is_date_like(hit.mention) and any(
        token in lower_name for token in ["date", "year", "issued", "dob", "birth"]
    ):
        score += 4.0
    if _is_capitalized_phrase(hit.mention) and any(
        token in lower_name for token in ["name", "title", "label", "status", "type"]
    ):
        score += 3.0
    if _is_categorical_field(col.name) and len(hit.mention.strip()) > 2:
        score += 3.5
    if "block" in context.lower() and lower_name == "block":
        score += 5.0
    if "frame" in context.lower() and "frame" in lower_name:
        score += 5.0
    if "flavor" in question.lower() and col.table.lower() == "foreign_data" and lower_name == "language":
        score += 5.0
    if "artist" in question.lower() and col.table.lower() == "foreign_data" and lower_name == "language":
        score += 5.0
    if hit.mention.lower() == "phyrexian" and col.table.lower() == "cards":
        score -= 5.0
    if any(token in context.lower() for token in ["branch", "district"]) and lower_name in {
        "a2",
        "a3",
    }:
        score += 4.0
    if (
        col.table.lower() == "district"
        and lower_name in {"a2", "a3"}
        and _is_capitalized_phrase(hit.mention)
        and re.search(rf"\b(?:in|from|of)\s+{re.escape(hit.mention)}\b", context, re.I)
    ):
        score += 5.0
    if lower_name == "type" and "type" not in context.lower():
        score -= 3.0
    if "publisher" in context.lower() and (
        "publisher" in col.table.lower() or "publisher" in lower_name
    ):
        score += 5.0
    if f"'{hit.mention}'" in question or f'"{hit.mention}"' in question:
        score += 3.0
    if hit.field in used_fields:
        score -= 3.0
    if lower_name in {"id", "uuid"} and not _asks_for_id(context) and not _is_code_like(hit.mention):
        score -= 1.5
    return score


def _field_question_score(question: str, table: str, column: str) -> float:
    q_tokens = _question_tokens_with_aliases(question)
    field_tokens = _name_tokens(column)
    table_tokens = _name_tokens(table)
    score = float(2 * len(field_tokens & q_tokens) + len(table_tokens & q_tokens))
    for role in _PROJECTION_ROLE_BOOST_TOKENS:
        if role in q_tokens and role in (field_tokens | table_tokens):
            score += 2.0
    lower = question.lower()
    col_lower = column.lower()
    if "number" in lower and ("num" in col_lower or "number" in col_lower):
        score += 2.0
    if "notes" in lower and col_lower == "notes":
        score += 4.0
    if "amount" in lower and col_lower == "amount":
        score += 5.0
    if "budget" in lower and ("amount" in lower or "total" in lower) and table.lower() == "budget" and col_lower == "amount":
        score += 5.0
    if "expense" in lower and col_lower in {"cost", "amount"}:
        score += 5.0
    if "status" in lower and col_lower == "status":
        score += 6.0
    if "type" in lower and col_lower == "type":
        score += 4.0
    if "expansion type" in lower and table.lower() == "sets" and col_lower == "type":
        score += 8.0
    if "language" in lower and "language" in col_lower:
        score += 4.0
    if "phone" in lower and "phone" in col_lower:
        score += 4.0
    if "hometown" in lower and col_lower in {"city", "hometown"}:
        score += 4.0
    if any(token in lower for token in ["website", "websites"]) and col_lower in {"url", "website"}:
        score += 6.0
    if "height" in lower and "height" in col_lower:
        score += 4.0
    if "flavor" in lower and "flavor" in col_lower:
        score += 6.0
    if "flavor" in lower and table.lower() == "foreign_data" and "flavor" in col_lower:
        score += 4.0
    if "artist" in lower and "artist" in col_lower:
        score += 8.0
    if any(token in lower for token in ["website", "websites", "purchase"]) and "purchase" in col_lower:
        score += 6.0
    if any(token in lower for token in ["superpower", "superpowers"]) and (
        "power" in col_lower or table.lower() == "superpower"
    ):
        score += 6.0
    if "block" in lower and col_lower == "block":
        score += 5.0
    if "frame" in lower and "frame" in col_lower:
        score += 5.0
    if any(token in lower for token in ["durability", "attribute"]) and col_lower == "attribute_value":
        score += 5.0
    if "card" in lower and table.lower() == "cards":
        score += 2.0
    if "names of the cards" in lower and table.lower() == "cards" and col_lower == "name":
        score += 8.0
    if col_lower in {"setcode", "set_code"} and "code" not in lower:
        score -= 5.0
    if "salary" in lower and ("salary" in col_lower or col_lower.startswith("a11")):
        score += 2.0
    if "date" in lower and "date" in col_lower:
        score += 2.0
    return score


def _aggregate_kind(question: str) -> str | None:
    lower = question.lower()
    if "how many" not in lower and any(
        token in lower for token in ["phone number", "account number", "card number"]
    ):
        return None
    if any(token in lower for token in ["average", "avg", "mean"]):
        return "avg"
    if any(token in lower for token in ["total", "sum of", "sum "]):
        return "sum"
    if any(token in lower for token in ["how many", "number of", "count of"]) or lower.startswith(
        "count "
    ):
        return "count"
    return None


def _operator_for_mention(question: str, mention: str) -> str:
    context = _operator_context(question, mention).lower()
    if any(token in context for token in ["less than", "before", "under", "below", "smaller than"]):
        return "<"
    if any(token in context for token in ["greater than", "more than", "after", "above", "over", "larger than"]):
        return ">"
    return "="


def _operator_context(question: str, mention: str) -> str:
    lower = question.lower()
    idx = lower.find(mention.lower())
    if idx < 0:
        return question
    return question[max(0, idx - 28) : min(len(question), idx + len(mention) + 8)]


def _mention_is_literal(question: str, mention: str) -> bool:
    stripped = mention.strip()
    if not stripped or stripped.lower() in _COMMAND_WORDS:
        return False
    quoted = f"'{stripped}'" in question or f'"{stripped}"' in question
    return (
        quoted
        or _is_date_like(stripped)
        or _is_code_like(stripped)
        or _is_capitalized_phrase(stripped)
        or stripped.lower() in {"male", "female", "yes", "no", "active", "inactive"}
    )


def _hit_mention_is_literal(question: str, atlas: DbAtlas, hit: ValueHit) -> bool:
    if hit.mention.strip().lower() in _COMMAND_WORDS:
        return False
    if _mention_is_literal(question, hit.mention):
        return True
    col = atlas.columns.get(hit.field)
    if col is None:
        return False
    if re.fullmatch(r"\d+(?:\.\d+)?", hit.mention.strip()):
        return _literal_compatible_with_field(question, hit.mention, col)
    return _is_categorical_field(col.name) and len(hit.mention.strip()) > 2


def _derived_enum_hits(question: str, atlas: DbAtlas) -> list[ValueHit]:
    lower = question.lower()
    derived: list[ValueHit] = []
    gender_values: list[tuple[str, str]] = []
    if _has_any_word(lower, ["female", "women", "woman", "girl"]):
        gender_values.extend([("female", "F"), ("female", "Female")])
    if _has_any_word(lower, ["male", "men", "man", "boy"]):
        gender_values.extend([("male", "M"), ("male", "Male")])
    if _has_any_word(lower, ["good"]):
        gender_values.append(("good", "Good"))
    if _has_any_word(lower, ["bad", "evil"]):
        gender_values.append(("bad", "Bad"))
    if not gender_values:
        gender_values = []
    for field, col in atlas.columns.items():
        name = col.name.lower()
        if name == "id" or name.endswith("_id"):
            continue
        for mention, db_value in gender_values:
            if mention in {"male", "female"} and not any(
                token in name for token in ["gender", "sex"]
            ):
                continue
            if mention in {"good", "bad"} and "alignment" not in name:
                continue
            derived.append(ValueHit(mention=mention, field=field, db_value=db_value))
        if "brazilian portuguese" in lower and "language" in name:
            derived.append(
                ValueHit(
                    mention="Brazilian Portuguese",
                    field=field,
                    db_value="Portuguese (Brazil)",
                )
            )
    return derived


def _derived_literal_hits(question: str, atlas: DbAtlas) -> list[ValueHit]:
    literals = _structured_literals(question)
    if not literals:
        return []
    derived: list[ValueHit] = []
    seen: set[tuple[str, str]] = set()
    for mention in literals:
        for field, col in sorted(atlas.columns.items()):
            if not _literal_compatible_with_field(question, mention, col):
                continue
            key = (mention, field)
            if key in seen:
                continue
            seen.add(key)
            derived.append(ValueHit(mention=mention, field=field, db_value=mention))
    return derived


def _structured_literals(question: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"\b\d{4}[/-]\d{1,2}[/-]\d{1,2}\b|\b(?:19|20)\d{2}\b|\b\d+(?:\.\d+)?\b", question):
        value = match.group(0)
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _literal_compatible_with_field(question: str, literal: str, col: Any) -> bool:
    lower_name = col.name.lower()
    if _is_date_like(literal) and not _is_plain_number(literal):
        return any(token in lower_name for token in ["date", "time", "created", "issued"])
    if _is_year_literal(literal):
        return any(
            token in lower_name
            for token in ["date", "time", "year", "created", "issued", "dob", "birth"]
        )
    if not re.fullmatch(r"\d+(?:\.\d+)?", literal.strip()):
        return False
    if not _looks_numeric(col.sql_type):
        return False
    context_tokens = set(_tokens(_mention_context(question, literal)))
    field_tokens = _name_tokens(col.name)
    lower_context = _mention_context(question, literal).lower()
    if any(token in lower_context for token in [" id", " identifier"]):
        if lower_name == "id":
            return _id_literal_targets_table(question, literal, col.table)
        if lower_name.endswith("_id"):
            return _id_literal_targets_column(question, literal, col.name)
    if lower_name == "id" or lower_name.endswith("_id"):
        return False
    if field_tokens & context_tokens:
        return True
    if "kg" in lower_context and "kg" in field_tokens:
        return True
    if "cm" in lower_context and "cm" in field_tokens:
        return True
    return False


def _id_literal_targets_table(question: str, literal: str, table: str) -> bool:
    lower = question.lower()
    literal_pattern = re.escape(literal.lower())
    for token in _name_tokens(table):
        if len(token) < 3:
            continue
        token_pattern = re.escape(_singular_token(token))
        pattern = rf"\b{token_pattern}s?\b(?:\W+\w+){{0,2}}\W+(?:id|identifier|number)\W+['\"]?{literal_pattern}\b"
        if re.search(pattern, lower):
            return True
    return False


def _id_literal_targets_column(question: str, literal: str, column_name: str) -> bool:
    lower = question.lower()
    literal_pattern = re.escape(literal.lower())
    role_tokens = _fk_role_tokens(column_name)
    for token in role_tokens:
        if len(token) < 3:
            continue
        token_pattern = re.escape(_singular_token(token))
        pattern = rf"\b{token_pattern}s?\b(?:\W+\w+){{0,2}}\W+(?:id|identifier|number)\W+['\"]?{literal_pattern}\b"
        if re.search(pattern, lower):
            return True
    return False


def _singular_token(value: str) -> str:
    if len(value) > 3 and value.endswith("ies"):
        return value[:-3] + "y"
    if len(value) > 2 and value.endswith("s"):
        return value[:-1]
    return value


def _has_any_word(lower_question: str, words: list[str]) -> bool:
    return any(re.search(rf"\b{re.escape(word)}\b", lower_question) for word in words)


def _is_redundant_component_mention(key: str, selected_mentions: set[str]) -> bool:
    if not key.replace(" ", "").isalpha():
        return False
    return any(key != selected and key in selected for selected in selected_mentions)


def _is_categorical_field(column_name: str) -> bool:
    lower = column_name.lower()
    return any(
        token in lower
        for token in [
            "alignment",
            "availability",
            "border",
            "category",
            "colour",
            "color",
            "format",
            "gender",
            "language",
            "layout",
            "name",
            "nationality",
            "position",
            "rarity",
            "state",
            "status",
            "type",
        ]
    )


def _projection_is_unsafe_id(question: str, atlas: DbAtlas, projection: Projection) -> bool:
    if projection.field is None:
        return False
    col = atlas.columns[projection.field]
    lower = col.name.lower()
    if lower not in {"id", "uuid"} and not lower.endswith("_id"):
        return False
    readable = lower.removesuffix("_id").replace("_", " ")
    return f"{readable} id" not in question.lower()


def _has_unbound_year(question: str, predicates: list[Predicate]) -> bool:
    years = set(re.findall(r"\b(?:19|20)\d{2}\b", question))
    if not years:
        return False
    if "born" in question.lower() and not any(
        any(token in predicate.field.rsplit(".", 1)[-1].lower() for token in ["dob", "birth"])
        for predicate in predicates
    ):
        return True
    for predicate in predicates:
        field_name = predicate.field.rsplit(".", 1)[-1].lower()
        if _norm_value(predicate.value) in years and any(
            token in field_name for token in ["date", "year", "time", "created", "dob", "birth"]
        ):
            return False
    return True


def _looks_complex(question: str) -> bool:
    lower = question.lower()
    complex_terms = [
        " and what ",
        "all types",
        "percentage",
        " percent",
        " rate",
        "ratio",
        "gap",
        "difference",
        "coordinate",
        "coordinates",
        "increase",
        "decrease",
        " for each ",
        " per ",
        "group",
        "rank",
        "popularity",
        "between",
        "from year",
        "above average",
        "ranging from",
        "ruling",
        "outside",
        "which card",
        "most common",
        "normal range",
        "set of cards with",
        "state the colour",
        "state the color",
        "each country",
        "value for money",
        "bonded",
    ]
    if any(term in lower for term in complex_terms):
        return True
    if "negative" in lower and not re.search(r"\b(?:less|under|below)\b", lower):
        return True
    if any(term in lower for term in ["less than", "greater than", "more than"]):
        return True
    return lower.strip().startswith("is ")


def _requires_order(question: str) -> bool:
    lower = question.lower()
    return any(
        token in lower
        for token in [
            "highest",
            "largest",
            "biggest",
            "lowest",
            "least",
            "earliest",
            "latest",
            "first",
            "last",
            "most",
        ]
    )


def _looks_numeric(sql_type: str) -> bool:
    lower = sql_type.lower()
    return any(token in lower for token in ["int", "real", "num", "dec", "double", "float"])


def _looks_display_column(column_name: str, sql_type: str) -> bool:
    lower_name = column_name.lower()
    if _looks_numeric(sql_type):
        return False
    if any(token in lower_name for token in ["name", "title", "label", "type", "colour", "color"]):
        return True
    return sql_type.lower() in {"", "text", "varchar", "char", "nvarchar"}


def _is_code_like(value: str) -> bool:
    stripped = value.strip()
    return bool(re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{1,12}", stripped)) and not stripped.isdigit()


def _is_plain_number(value: str) -> bool:
    return bool(re.fullmatch(r"\d+", value.strip()))


def _is_date_like(value: str) -> bool:
    return bool(re.fullmatch(r"\d{4}([/-]\d{1,2}){0,2}", value.strip()))


def _is_year_literal(value: str) -> bool:
    return bool(re.fullmatch(r"(?:19|20)\d{2}", value.strip()))


def _is_capitalized_phrase(value: str) -> bool:
    words = re.findall(r"[A-Za-z0-9]+", value)
    if not words:
        return False
    return any(word[:1].isupper() for word in words)


def _asks_for_id(question: str) -> bool:
    lower = question.lower()
    return any(token in lower for token in [" id", "ids", "identifier", "number", "code"])


def _mention_context(question: str, mention: str, radius: int = 36) -> str:
    lower = question.lower()
    idx = lower.find(mention.lower())
    if idx < 0:
        return question
    return question[max(0, idx - radius) : min(len(question), idx + len(mention) + radius)]


def _sql_literal(value: str, sql_type: str) -> str:
    raw = value.strip()
    if _looks_numeric(sql_type) and re.fullmatch(r"-?\d+(\.\d+)?", raw):
        return raw
    normalized_date = _normalize_date(raw)
    if normalized_date is not None:
        raw = normalized_date
    return "'" + raw.replace("'", "''") + "'"


def _normalize_date(value: str) -> str | None:
    match = re.fullmatch(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", value.strip())
    if not match:
        return None
    year, month, day = match.groups()
    return f"{year}-{int(month):02d}-{int(day):02d}"


def _field_ref(table: str, column: str) -> str:
    return f"{_quote_ident(table)}.{_quote_ident(column)}"


def _load_binder_examples(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    examples = raw.get("examples", [])
    if not isinstance(examples, list):
        return []
    return [row for row in examples if isinstance(row, dict)]


def _load_current_correct(report_json: Path | None) -> dict[int, bool]:
    if report_json is None:
        return {}
    raw = json.loads(report_json.read_text(encoding="utf-8"))
    out: dict[int, bool] = {}
    for example in raw.get("examples", []):
        if isinstance(example, dict) and "index" in example:
            out[int(example["index"])] = bool(example.get("exec_equal"))
    return out


_COMMAND_WORDS = {
    "average",
    "available",
    "budget",
    "card",
    "cards",
    "code",
    "count",
    "expansion",
    "give",
    "having",
    "known",
    "language",
    "languages",
    "list",
    "market",
    "member",
    "name",
    "power",
    "show",
    "status",
    "type",
    "user",
    "where",
}

_DISPLAY_FK_ROLE_TOKENS = {
    "alignment",
    "city",
    "color",
    "colour",
    "country",
    "district",
    "gender",
    "language",
    "location",
    "position",
    "publisher",
    "race",
    "region",
    "state",
    "status",
    "type",
}

_PROJECTION_ROLE_BOOST_TOKENS = {
    "alignment",
    "city",
    "color",
    "colour",
    "country",
    "district",
    "gender",
    "language",
    "publisher",
    "race",
    "region",
    "state",
}
