"""Post-run diagnostics for Spider/BIRD per-example reports.

The eval CLI already tells us whether a prediction executed correctly.
This module looks one layer deeper and compares the *shape* of the gold
SQL to the predicted SQL so operators can tell which cascade stage is
still gating progress:

- schema/linker: wrong or missing table set / joins
- skeleton: missing clauses, arithmetic, DISTINCT, ranking/limit shape
- slot/value: literal and comparison-value mismatches
- runtime: execution errors, timeouts, structural failures

The implementation is intentionally heuristic. It is not a SQL parser;
it is a stable triage pass over eval reports so BIRD smoke runs do not
turn into one-off spreadsheet archaeology.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "AcceptedSqlExample",
    "DiagnosisExample",
    "DiagnosisReport",
    "ProductSafetyDiagnostics",
    "SqlFeatures",
    "diagnose_report",
    "diagnosis_report_to_json",
    "render_diagnosis_markdown",
]


_FEATURE_ORDER = (
    "where",
    "join",
    "distinct",
    "arithmetic",
    "group_by",
    "having",
    "order_by",
    "limit",
    "subquery",
)
_AGG_RE = re.compile(r"\b(COUNT|SUM|AVG|MIN|MAX)\s*\(", re.IGNORECASE)
_CLAUSE_RE = {
    "where": re.compile(r"\bWHERE\b", re.IGNORECASE),
    "join": re.compile(r"\bJOIN\b", re.IGNORECASE),
    "distinct": re.compile(r"\bDISTINCT\b", re.IGNORECASE),
    "group_by": re.compile(r"\bGROUP\s+BY\b", re.IGNORECASE),
    "having": re.compile(r"\bHAVING\b", re.IGNORECASE),
    "order_by": re.compile(r"\bORDER\s+BY\b", re.IGNORECASE),
    "limit": re.compile(r"\bLIMIT\b", re.IGNORECASE),
    "subquery": re.compile(r"\(\s*SELECT\b", re.IGNORECASE),
}
_TABLE_RE = re.compile(
    r"\b(?:FROM|JOIN)\s+([`\"\[]?[A-Za-z_][\w$]*(?:[`\"\]]?)?)",
    re.IGNORECASE,
)
_STRING_RE = re.compile(r"'([^']*)'")
_NUMBER_RE = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])")


@dataclass(frozen=True)
class SqlFeatures:
    """Lightweight feature sketch for one SQL string."""

    tables: list[str]
    values: list[str]
    numbers: list[str]
    projection_count: int
    where: bool = False
    join: bool = False
    distinct: bool = False
    arithmetic: bool = False
    group_by: bool = False
    having: bool = False
    order_by: bool = False
    limit: bool = False
    subquery: bool = False
    aggregate: bool = False
    count: bool = False
    select_star: bool = False


@dataclass(frozen=True)
class DiagnosisExample:
    """One failing example with diagnostic tags."""

    index: int
    db_id: str
    question: str
    failure_bucket: str
    tags: list[str]
    lanes: list[str]
    gold_sql: str
    pred_sql: str


@dataclass(frozen=True)
class AcceptedSqlExample:
    """One wrong final-SQL example surfaced for product-safety triage."""

    index: int
    db_id: str
    question: str
    acceptance_kind: str
    stage_pinned: str
    route_reason: str
    failure_bucket: str
    pred_sql: str


@dataclass(frozen=True)
class ProductSafetyDiagnostics:
    """Counts that separate bad accuracy from unsafe product acceptance."""

    final_sql_total: int
    final_sql_correct: int
    final_sql_wrong: int
    route_used_total: int
    route_used_correct: int
    route_used_wrong: int
    model_sql_after_route_reject_total: int
    model_sql_after_route_reject_wrong: int
    routed_not_used_total: int
    routed_not_used_wrong: int
    by_db: dict[str, dict[str, int]]
    wrong_examples: list[AcceptedSqlExample] = field(default_factory=list)


@dataclass(frozen=True)
class DiagnosisReport:
    """Aggregate diagnostics for a per-example eval report."""

    source_report: str
    suite: str
    total: int
    correct: int
    exec_acc: float
    lane_counts: dict[str, int]
    tag_counts: dict[str, int]
    feature_gaps: dict[str, int]
    gold_feature_counts: dict[str, int]
    pred_feature_counts: dict[str, int]
    by_db: dict[str, dict[str, Any]]
    product_safety: ProductSafetyDiagnostics
    examples: list[DiagnosisExample] = field(default_factory=list)


def diagnose_report(
    report_json: Path,
    *,
    sample_examples: int = 20,
) -> DiagnosisReport:
    """Load an eval report JSON and classify per-example failure shape."""

    raw = json.loads(report_json.read_text(encoding="utf-8"))
    summary = raw.get("summary") or {}
    records = raw.get("examples") or []
    if not isinstance(records, list):
        raise ValueError(f"{report_json}: expected examples to be a list")

    lane_counts: dict[str, int] = {}
    tag_counts: dict[str, int] = {}
    feature_gaps: dict[str, int] = {}
    gold_feature_counts: dict[str, int] = {k: 0 for k in _FEATURE_ORDER}
    pred_feature_counts: dict[str, int] = {k: 0 for k in _FEATURE_ORDER}
    by_db: dict[str, dict[str, Any]] = {}
    examples: list[DiagnosisExample] = []

    for idx, rec in enumerate(records):
        if not isinstance(rec, dict):
            continue
        gold_sql = _str_or_empty(rec.get("gold_sql"))
        pred_sql = _str_or_empty(rec.get("pred_sql"))
        gold = _extract_features(gold_sql)
        pred = _extract_features(pred_sql)

        for name in _FEATURE_ORDER:
            if getattr(gold, name):
                gold_feature_counts[name] += 1
            if getattr(pred, name):
                pred_feature_counts[name] += 1

        tags, lanes = _classify_example(rec, gold, pred)
        db_id = _str_or_empty(rec.get("db_id")) or "<missing>"
        db_stats = by_db.setdefault(
            db_id,
            {
                "total": 0,
                "correct": 0,
                "wrong": 0,
                "failure_buckets": {},
                "lanes": {},
                "tags": {},
            },
        )
        db_stats["total"] += 1
        if bool(rec.get("exec_equal")):
            db_stats["correct"] += 1
        else:
            db_stats["wrong"] += 1
        bucket = _str_or_empty(rec.get("failure_bucket")) or "<missing>"
        _bump_nested_count(db_stats, "failure_buckets", bucket)
        for tag in tags:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
            _bump_nested_count(db_stats, "tags", tag)
            if tag.startswith("missing_") or tag.startswith("extra_"):
                feature_gaps[tag] = feature_gaps.get(tag, 0) + 1
        for lane in lanes:
            lane_counts[lane] = lane_counts.get(lane, 0) + 1
            _bump_nested_count(db_stats, "lanes", lane)

        if tags and len(examples) < sample_examples:
            examples.append(
                DiagnosisExample(
                    index=idx,
                    db_id=_str_or_empty(rec.get("db_id")),
                    question=_str_or_empty(rec.get("question")),
                    failure_bucket=_str_or_empty(rec.get("failure_bucket")),
                    tags=tags,
                    lanes=lanes,
                    gold_sql=gold_sql,
                    pred_sql=pred_sql,
                )
            )

    return DiagnosisReport(
        source_report=str(report_json),
        suite=_str_or_empty(summary.get("suite")),
        total=int(summary.get("total") or len(records)),
        correct=int(summary.get("correct") or 0),
        exec_acc=float(summary.get("exec_acc") or 0.0),
        lane_counts=dict(sorted(lane_counts.items())),
        tag_counts=dict(sorted(tag_counts.items())),
        feature_gaps=dict(sorted(feature_gaps.items())),
        gold_feature_counts=gold_feature_counts,
        pred_feature_counts=pred_feature_counts,
        by_db=_sorted_db_stats(by_db),
        product_safety=_product_safety_diagnostics(
            records,
            sample_examples=sample_examples,
        ),
        examples=examples,
    )


def diagnosis_report_to_json(report: DiagnosisReport) -> str:
    return json.dumps(asdict(report), indent=2, sort_keys=True)


def render_diagnosis_markdown(report: DiagnosisReport) -> str:
    """Render a concise Markdown diagnosis."""

    lines = [
        f"# SemanticSQL Report Diagnosis ({report.suite or 'unknown'})",
        "",
        f"- source: `{report.source_report}`",
        f"- total: `{report.total}`",
        f"- correct: `{report.correct}`",
        f"- exec_acc: `{report.exec_acc:.3%}`",
        "",
        "## Product Safety",
        "",
        _render_product_safety_summary(report.product_safety),
        "",
        "## Fix Lanes",
        "",
    ]
    if report.lane_counts:
        for lane, count in _sorted_counts(report.lane_counts):
            lines.append(f"- `{lane}`: {count}")
    else:
        lines.append("- `<none>`: 0")

    lines.extend(["", "## Feature Pressure", "", "| feature | gold | pred |", "|---|---:|---:|"])
    for name in _FEATURE_ORDER:
        lines.append(
            f"| `{name}` | {report.gold_feature_counts.get(name, 0)} "
            f"| {report.pred_feature_counts.get(name, 0)} |"
        )

    lines.extend(["", "## By DB", "", "| db_id | total | correct | wrong | exec_acc | top bucket | top lane |", "|---|---:|---:|---:|---:|---|---|"])
    for db_id, stats in report.by_db.items():
        total = int(stats.get("total") or 0)
        correct = int(stats.get("correct") or 0)
        wrong = int(stats.get("wrong") or 0)
        exec_acc = (correct / total) if total else 0.0
        bucket = _top_count(stats.get("failure_buckets") or {}, exclude={"correct"})
        lane = _top_count(stats.get("lanes") or {})
        lines.append(
            f"| `{db_id}` | {total} | {correct} | {wrong} | {exec_acc:.3%} "
            f"| `{bucket}` | `{lane}` |"
        )

    lines.extend(["", "## Failure Tags", ""])
    if report.tag_counts:
        for tag, count in _sorted_counts(report.tag_counts):
            lines.append(f"- `{tag}`: {count}")
    else:
        lines.append("- `<none>`: 0")

    lines.extend(["", "## Recommended Next Moves", ""])
    lines.extend(_recommendations(report))

    if report.examples:
        lines.extend(["", "## Sample Failures", ""])
        for ex in report.examples:
            lines.extend(
                [
                    f"### {ex.index}: {ex.db_id}",
                    "",
                    f"- question: {ex.question}",
                    f"- bucket: `{ex.failure_bucket}`",
                    f"- lanes: {', '.join(f'`{lane}`' for lane in ex.lanes)}",
                    f"- tags: {', '.join(f'`{tag}`' for tag in ex.tags)}",
                    "",
                    "```sql",
                    f"-- gold\n{ex.gold_sql}",
                    f"-- pred\n{ex.pred_sql}",
                    "```",
                    "",
                ]
            )

    return "\n".join(lines).rstrip() + "\n"


def _product_safety_diagnostics(
    records: list[Any],
    *,
    sample_examples: int,
) -> ProductSafetyDiagnostics:
    final_sql_total = 0
    final_sql_correct = 0
    final_sql_wrong = 0
    route_used_total = 0
    route_used_correct = 0
    route_used_wrong = 0
    model_sql_after_route_reject_total = 0
    model_sql_after_route_reject_wrong = 0
    routed_not_used_total = 0
    routed_not_used_wrong = 0
    by_db: dict[str, dict[str, int]] = {}
    route_used_wrong_examples: list[AcceptedSqlExample] = []
    other_wrong_examples: list[AcceptedSqlExample] = []

    for idx, rec in enumerate(records):
        if not isinstance(rec, dict):
            continue
        pred_sql = _str_or_empty(rec.get("pred_sql")).strip()
        if not _is_emitted_final_sql(rec, pred_sql):
            continue
        runtime = rec.get("runtime_query_frame")
        runtime_frame = runtime if isinstance(runtime, dict) else {}
        correct = bool(rec.get("exec_equal"))
        db_id = _str_or_empty(rec.get("db_id")) or "<missing>"
        db_stats = by_db.setdefault(db_id, _empty_product_safety_db_stats())
        route_used = runtime_frame.get("used_for_final_sql") is True
        routed = runtime_frame.get("routed") is True
        route_rejected = runtime_frame.get("routed") is False
        routed_not_used = routed and not route_used
        model_sql_after_reject = route_rejected and not route_used

        final_sql_total += 1
        db_stats["final_sql_total"] += 1
        if correct:
            final_sql_correct += 1
            db_stats["final_sql_correct"] += 1
        else:
            final_sql_wrong += 1
            db_stats["final_sql_wrong"] += 1

        if route_used:
            route_used_total += 1
            db_stats["route_used_total"] += 1
            if correct:
                route_used_correct += 1
                db_stats["route_used_correct"] += 1
            else:
                route_used_wrong += 1
                db_stats["route_used_wrong"] += 1
        if model_sql_after_reject:
            model_sql_after_route_reject_total += 1
            db_stats["model_sql_after_route_reject_total"] += 1
            if not correct:
                model_sql_after_route_reject_wrong += 1
                db_stats["model_sql_after_route_reject_wrong"] += 1
        if routed_not_used:
            routed_not_used_total += 1
            db_stats["routed_not_used_total"] += 1
            if not correct:
                routed_not_used_wrong += 1
                db_stats["routed_not_used_wrong"] += 1

        if not correct:
            example = AcceptedSqlExample(
                index=idx,
                db_id=db_id,
                question=_str_or_empty(rec.get("question")),
                acceptance_kind=_acceptance_kind(
                    route_used=route_used,
                    model_sql_after_reject=model_sql_after_reject,
                    routed_not_used=routed_not_used,
                ),
                stage_pinned=_str_or_empty(rec.get("stage_pinned")),
                route_reason=_str_or_empty(runtime_frame.get("route_reason")),
                failure_bucket=_str_or_empty(rec.get("failure_bucket")),
                pred_sql=pred_sql,
            )
            if route_used:
                route_used_wrong_examples.append(example)
            else:
                other_wrong_examples.append(example)

    return ProductSafetyDiagnostics(
        final_sql_total=final_sql_total,
        final_sql_correct=final_sql_correct,
        final_sql_wrong=final_sql_wrong,
        route_used_total=route_used_total,
        route_used_correct=route_used_correct,
        route_used_wrong=route_used_wrong,
        model_sql_after_route_reject_total=model_sql_after_route_reject_total,
        model_sql_after_route_reject_wrong=model_sql_after_route_reject_wrong,
        routed_not_used_total=routed_not_used_total,
        routed_not_used_wrong=routed_not_used_wrong,
        by_db=_sorted_product_safety_db_stats(by_db),
        wrong_examples=[
            *(route_used_wrong_examples[:sample_examples]),
            *other_wrong_examples[: max(0, sample_examples - len(route_used_wrong_examples))],
        ],
    )


def _empty_product_safety_db_stats() -> dict[str, int]:
    return {
        "final_sql_total": 0,
        "final_sql_correct": 0,
        "final_sql_wrong": 0,
        "route_used_total": 0,
        "route_used_correct": 0,
        "route_used_wrong": 0,
        "model_sql_after_route_reject_total": 0,
        "model_sql_after_route_reject_wrong": 0,
        "routed_not_used_total": 0,
        "routed_not_used_wrong": 0,
    }


def _is_emitted_final_sql(rec: dict[str, Any], pred_sql: str) -> bool:
    if not pred_sql or bool(rec.get("bailed")):
        return False
    if bool(rec.get("exec_equal")):
        return True
    bucket = _str_or_empty(rec.get("failure_bucket"))
    if bucket in {"exec_mismatch", "pred_exec_error"}:
        return True
    return bucket == "timeout" and bool(rec.get("pred_timeout"))


def _acceptance_kind(
    *,
    route_used: bool,
    model_sql_after_reject: bool,
    routed_not_used: bool,
) -> str:
    if route_used:
        return "route_used"
    if model_sql_after_reject:
        return "model_sql_after_route_reject"
    if routed_not_used:
        return "routed_not_used"
    return "final_sql"


def _sorted_product_safety_db_stats(
    by_db: dict[str, dict[str, int]],
) -> dict[str, dict[str, int]]:
    return dict(
        sorted(
            by_db.items(),
            key=lambda item: (-item[1].get("final_sql_wrong", 0), item[0]),
        )
    )


def _render_product_safety_summary(product: ProductSafetyDiagnostics) -> str:
    lines = [
        "| signal | total | correct | wrong |",
        "|---|---:|---:|---:|",
        (
            f"| final SQL emitted | {product.final_sql_total} "
            f"| {product.final_sql_correct} | {product.final_sql_wrong} |"
        ),
        (
            f"| route used for final SQL | {product.route_used_total} "
            f"| {product.route_used_correct} | {product.route_used_wrong} |"
        ),
        (
            f"| model SQL after route reject | "
            f"{product.model_sql_after_route_reject_total} | - "
            f"| {product.model_sql_after_route_reject_wrong} |"
        ),
        (
            f"| routed but not used | {product.routed_not_used_total} | - "
            f"| {product.routed_not_used_wrong} |"
        ),
    ]
    if product.by_db:
        lines.extend(
            [
                "",
                "| db_id | final wrong | route-used wrong | model-after-reject wrong |",
                "|---|---:|---:|---:|",
            ]
        )
        for db_id, stats in product.by_db.items():
            lines.append(
                f"| `{db_id}` | {stats.get('final_sql_wrong', 0)} "
                f"| {stats.get('route_used_wrong', 0)} "
                f"| {stats.get('model_sql_after_route_reject_wrong', 0)} |"
            )
    if product.wrong_examples:
        lines.extend(["", "### Accepted Wrong SQL Samples", ""])
        for example in product.wrong_examples:
            lines.extend(
                [
                    f"- `{example.index}` `{example.db_id}` "
                    f"`{example.acceptance_kind}` `{example.route_reason or '-'}`: "
                    f"{example.question}",
                    f"  - stage: `{example.stage_pinned or '-'}`; "
                    f"bucket: `{example.failure_bucket or '-'}`",
                ]
            )
    return "\n".join(lines)


def _classify_example(
    rec: dict[str, Any],
    gold: SqlFeatures,
    pred: SqlFeatures,
) -> tuple[list[str], list[str]]:
    tags: list[str] = []
    lanes: list[str] = []

    bucket = _str_or_empty(rec.get("failure_bucket"))
    if bucket in {
        "timeout",
        "pred_exec_error",
        "cascade_error",
        "stage2_constraint_error",
        "stage2_structural_error",
        "stage4_render_error",
    }:
        tags.append(bucket)
        lanes.append("runtime_contract")
        return tags, _dedupe(lanes)

    if bool(rec.get("exec_equal")):
        return tags, lanes

    for name in _FEATURE_ORDER:
        gold_has = bool(getattr(gold, name))
        pred_has = bool(getattr(pred, name))
        if gold_has and not pred_has:
            tags.append(f"missing_{name}")
        elif pred_has and not gold_has:
            tags.append(f"extra_{name}")

    gold_tables = {t.lower() for t in gold.tables}
    pred_tables = {t.lower() for t in pred.tables}
    missing_tables = gold_tables - pred_tables
    extra_tables = pred_tables - gold_tables
    if missing_tables:
        tags.append("missing_table")
    if extra_tables:
        tags.append("extra_table")
    if len(pred_tables) > len(gold_tables):
        tags.append("over_joined_table_set")
    elif len(pred_tables) < len(gold_tables):
        tags.append("under_joined_table_set")

    if gold.arithmetic and pred.count:
        tags.append("ratio_or_metric_collapsed_to_count")
    if gold.projection_count > 0 and pred.select_star:
        tags.append("projection_collapsed_to_star")
    if gold.limit and not pred.order_by:
        tags.append("ranking_shape_missing_order")
    if gold.distinct and pred.count and "missing_distinct" in tags:
        tags.append("count_missing_distinct")

    gold_values = set(_normalise_values(gold.values + gold.numbers))
    pred_values = set(_normalise_values(pred.values + pred.numbers))
    if gold_values and not gold_values.issubset(pred_values):
        tags.append("value_mismatch")
    if pred_values and not pred_values.issubset(gold_values):
        tags.append("extra_pred_value")
    if _numeric_comparison_has_quoted_word(_str_or_empty(rec.get("pred_sql"))):
        tags.append("typed_value_mismatch")

    if any(tag in tags for tag in ("missing_join", "missing_table", "under_joined_table_set")):
        lanes.append("schema_linker_or_join_planning")
    if any(tag in tags for tag in ("extra_join", "extra_table", "over_joined_table_set")):
        lanes.append("schema_pruning_or_join_minimality")
    if any(
        tag in tags
        for tag in (
            "missing_arithmetic",
            "missing_order_by",
            "missing_limit",
            "missing_distinct",
            "missing_group_by",
            "missing_having",
            "ratio_or_metric_collapsed_to_count",
            "projection_collapsed_to_star",
            "ranking_shape_missing_order",
        )
    ):
        lanes.append("skeleton_planning")
    if any(
        tag in tags
        for tag in ("value_mismatch", "extra_pred_value", "typed_value_mismatch")
    ):
        lanes.append("slot_value_grounding")
    if not lanes and tags:
        lanes.append("semantic_mismatch")

    return _dedupe(tags), _dedupe(lanes)


def _bump_nested_count(stats: dict[str, Any], key: str, name: str) -> None:
    nested = stats.setdefault(key, {})
    if not isinstance(nested, dict):
        return
    nested[name] = int(nested.get(name, 0)) + 1


def _sorted_db_stats(by_db: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    def sort_key(item: tuple[str, dict[str, Any]]) -> tuple[int, str]:
        db_id, stats = item
        return (-int(stats.get("wrong") or 0), db_id)

    out: dict[str, dict[str, Any]] = {}
    for db_id, stats in sorted(by_db.items(), key=sort_key):
        out[db_id] = {
            **stats,
            "failure_buckets": dict(sorted((stats.get("failure_buckets") or {}).items())),
            "lanes": dict(sorted((stats.get("lanes") or {}).items())),
            "tags": dict(sorted((stats.get("tags") or {}).items())),
        }
    return out


def _top_count(counts: Any, *, exclude: set[str] | None = None) -> str:
    if not isinstance(counts, dict) or not counts:
        return "<none>"
    excluded = exclude or set()
    filtered = [
        (str(name), int(count))
        for name, count in counts.items()
        if str(name) not in excluded
    ]
    if not filtered:
        return "<none>"
    name, count = sorted(filtered, key=lambda item: (-item[1], item[0]))[0]
    return f"{name} ({count})"


def _extract_features(sql: str) -> SqlFeatures:
    compact = " ".join(sql.split())
    tables = _dedupe(_strip_identifier(t) for t in _TABLE_RE.findall(compact))
    values = _dedupe(match.group(1) for match in _STRING_RE.finditer(compact))
    numbers = _dedupe(match.group(0) for match in _NUMBER_RE.finditer(compact))
    select_star = bool(re.search(r"\bSELECT\s+\*", compact, re.IGNORECASE))
    projection_count = _projection_count(compact)
    arithmetic = _has_arithmetic(compact)
    aggregate = bool(_AGG_RE.search(compact))
    count = bool(re.search(r"\bCOUNT\s*\(", compact, re.IGNORECASE))
    clauses = {
        name: bool(pattern.search(compact))
        for name, pattern in _CLAUSE_RE.items()
    }
    return SqlFeatures(
        tables=tables,
        values=values,
        numbers=numbers,
        projection_count=projection_count,
        arithmetic=arithmetic,
        aggregate=aggregate,
        count=count,
        select_star=select_star,
        **clauses,
    )


def _projection_count(sql: str) -> int:
    match = re.search(r"\bSELECT\b(.*?)\bFROM\b", sql, re.IGNORECASE)
    if not match:
        return 0
    select_part = match.group(1).strip()
    if not select_part:
        return 0
    depth = 0
    count = 1
    for ch in select_part:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            count += 1
    return count


def _has_arithmetic(sql: str) -> bool:
    if re.search(r"\bCAST\s*\(", sql, re.IGNORECASE):
        return True
    select_expr = re.search(r"\bSELECT\b(.*?)\bFROM\b", sql, re.IGNORECASE)
    where_expr = re.search(
        r"\bWHERE\b(.*?)(?:\bGROUP\s+BY\b|\bHAVING\b|\bORDER\s+BY\b|\bLIMIT\b|$)",
        sql,
        re.IGNORECASE,
    )
    for part in (select_expr.group(1) if select_expr else "", where_expr.group(1) if where_expr else ""):
        if re.search(r"[A-Za-z_`\"\)]\s*[/*+]\s*[A-Za-z_`\"\(]", part):
            return True
    return False


def _numeric_comparison_has_quoted_word(sql: str) -> bool:
    return bool(
        re.search(
            r"(?:>|<|>=|<=|BETWEEN)\s*'[^']*[A-Za-z_][^']*'",
            sql,
            re.IGNORECASE,
        )
    )


def _normalise_values(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        cleaned = value.strip().strip("'\"`").lower()
        if cleaned:
            out.append(cleaned)
    return out


def _strip_identifier(identifier: str) -> str:
    return identifier.strip().strip("`\"[]")


def _str_or_empty(value: object) -> str:
    return value if isinstance(value, str) else ""


def _dedupe(items: Any) -> list[str]:
    out: list[str] = []
    for item in items:
        s = str(item)
        if s and s not in out:
            out.append(s)
    return out


def _sorted_counts(counts: dict[str, int]) -> list[tuple[str, int]]:
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def _recommendations(report: DiagnosisReport) -> list[str]:
    lane_counts = report.lane_counts
    tag_counts = report.tag_counts
    product = report.product_safety
    recs: list[str] = []
    skeleton = lane_counts.get("skeleton_planning", 0)
    schema = (
        lane_counts.get("schema_linker_or_join_planning", 0)
        + lane_counts.get("schema_pruning_or_join_minimality", 0)
    )
    slots = lane_counts.get("slot_value_grounding", 0)
    runtime = lane_counts.get("runtime_contract", 0)

    if skeleton:
        recs.append(
            "- Prioritize Stage 2 skeleton training/eval gates for JOIN, arithmetic, "
            "ORDER BY, LIMIT, DISTINCT, and shallow COUNT/SELECT-star collapse."
        )
    if schema:
        recs.append(
            "- Improve Stage 1 schema recall with controlled reranking: keep high recall "
            "candidates nearby, then prune joins by FK reachability and minimal table set."
        )
    if slots:
        recs.append(
            "- Add or tighten DB-value/evidence retrieval before Stage 3 so literals and "
            "typed comparisons are grounded against column-compatible candidates."
        )
    if runtime:
        recs.append(
            "- Fix runtime/contract errors before interpreting semantic accuracy; execution "
            "errors hide downstream model quality."
        )
    if product.route_used_wrong:
        recs.append(
            "- Fail closed before deterministic route promotion when route evidence is "
            "incomplete; route-used wrong SQL is a product-safety blocker."
        )
    if product.model_sql_after_route_reject_wrong:
        recs.append(
            "- Route rejected questions through typed fallback or clarification before "
            "emitting Stage 3 SQL; rejected-route wrong SQL should not ship."
        )
    if tag_counts.get("ratio_or_metric_collapsed_to_count", 0):
        recs.append(
            "- Add ratio/metric templates or hard negatives so rate questions do not decode "
            "as COUNT aggregates."
        )
    if tag_counts.get("over_joined_table_set", 0):
        recs.append(
            "- Add a join-minimality check for generated candidates; extra bridge tables are "
            "valid SQL but usually break execution equality."
        )
    if not recs:
        recs.append("- No recurring failure lane detected in this report.")
    return recs
