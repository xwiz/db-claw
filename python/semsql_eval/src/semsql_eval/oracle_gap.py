"""Oracle-vs-live recovery diagnostics.

This report compares a live Stage 3 run against an all-oracle upper-bound
run and the teacher cache that defines coverage. It answers a narrower
question than generic SQL diagnosis: how much of the oracle-covered,
currently-solvable set does live Stage 3 recover?
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "OracleGapExample",
    "OracleGapReport",
    "canonicalize_slot_value",
    "oracle_gap_report",
    "oracle_gap_report_to_json",
    "render_oracle_gap_markdown",
]


@dataclass(frozen=True)
class OracleGapExample:
    index: int
    db_id: str
    question: str
    partition: str
    current_correct: bool
    oracle_correct: bool
    covered: bool
    current_sql: str
    oracle_sql: str
    gold_sql: str
    slot_gap_bucket: str = "not_applicable"
    slot_mismatches: int = 0
    slot_gold_missing: int = 0
    slot_pre_stage3_missing: int = 0
    slot_lost_before_scoring: int = 0
    slot_present_misranked: int = 0
    slot_selected_binding_available: int = 0
    slot_selected_binding_missing: int = 0


@dataclass(frozen=True)
class OracleGapReport:
    current_report: str
    oracle_report: str
    oracle_cache: str
    previous_report: str | None
    total: int
    oracle_cache_coverage: float
    covered_total: int
    all_oracle_covered_correct: int
    all_oracle_covered_acc: float
    live_stage3_recovered: int
    live_stage3_recovery: float
    regressions_from_previous: int
    partitions: dict[str, int]
    by_db: dict[str, dict[str, int | float]]
    slot_totals_by_kind: dict[str, int]
    slot_mismatches_by_kind: dict[str, int]
    slot_gold_missing_by_kind: dict[str, int]
    slot_pre_stage3_missing_by_kind: dict[str, int]
    slot_lost_before_scoring_by_kind: dict[str, int]
    slot_present_misranked_by_kind: dict[str, int]
    slot_selected_binding_available_by_kind: dict[str, int]
    slot_selected_binding_missing_by_kind: dict[str, int]
    oracle_only_triage: dict[str, int]
    examples: list[OracleGapExample] = field(default_factory=list)


def oracle_gap_report(
    *,
    current_report_json: Path,
    oracle_report_json: Path,
    oracle_cache_jsonl: Path,
    previous_report_json: Path | None = None,
    sample_examples: int = 20,
) -> OracleGapReport:
    current = _load_report(current_report_json)
    oracle = _load_report(oracle_report_json)
    previous = _load_report(previous_report_json) if previous_report_json else {}
    cache = _load_oracle_cache(oracle_cache_jsonl)

    oracle_records = _records_by_key(oracle)
    previous_records = _records_by_key(previous)

    partitions = {
        "both_correct": 0,
        "oracle_only": 0,
        "both_wrong_covered": 0,
        "current_only": 0,
        "uncovered": 0,
    }
    by_db: dict[str, dict[str, int | float]] = {}
    slot_totals: dict[str, int] = {}
    slot_mismatches: dict[str, int] = {}
    slot_gold_missing: dict[str, int] = {}
    slot_pre_stage3_missing: dict[str, int] = {}
    slot_lost_before_scoring: dict[str, int] = {}
    slot_present_misranked: dict[str, int] = {}
    slot_selected_binding_available: dict[str, int] = {}
    slot_selected_binding_missing: dict[str, int] = {}
    oracle_only_triage = {
        "gold_absent_from_pre_stage3": 0,
        "gold_absent_from_candidates": 0,
        "gold_lost_before_scoring": 0,
        "present_but_misranked": 0,
        "all_slots_match": 0,
        "missing_stage3_trace": 0,
    }
    examples: list[OracleGapExample] = []
    regressions = 0

    for index, current_rec in enumerate(current.get("examples") or []):
        if not isinstance(current_rec, dict):
            continue
        key = _example_key(current_rec)
        oracle_rec = oracle_records.get(key, {})
        previous_rec = previous_records.get(key, {})
        cache_rec = cache.get(key)
        covered = cache_rec is not None
        current_correct = bool(current_rec.get("exec_equal"))
        oracle_correct = bool(oracle_rec.get("exec_equal"))
        previous_correct = bool(previous_rec.get("exec_equal"))
        db_id = str(current_rec.get("db_id") or "")
        slot_gap = _slot_gap_stats(current_rec, cache_rec or {}) if covered else {}

        if previous_correct and not current_correct:
            regressions += 1

        if not covered:
            partition = "uncovered"
        elif oracle_correct and current_correct:
            partition = "both_correct"
        elif oracle_correct and not current_correct:
            partition = "oracle_only"
            _accumulate_slot_stats(
                slot_gap,
                slot_totals,
                slot_mismatches,
                slot_gold_missing,
                slot_pre_stage3_missing,
                slot_lost_before_scoring,
                slot_present_misranked,
                slot_selected_binding_available,
                slot_selected_binding_missing,
            )
            bucket = str(slot_gap.get("bucket") or "missing_stage3_trace")
            oracle_only_triage[bucket] = oracle_only_triage.get(bucket, 0) + 1
        elif current_correct:
            partition = "current_only"
        else:
            partition = "both_wrong_covered"

        partitions[partition] += 1
        db_stats = by_db.setdefault(
            db_id,
            {
                "total": 0,
                "covered": 0,
                "all_oracle_correct": 0,
                "current_correct": 0,
                "both_correct": 0,
                "oracle_only": 0,
                "both_wrong_covered": 0,
                "current_only": 0,
                "uncovered": 0,
                "live_stage3_recovery": 0.0,
            },
        )
        db_stats["total"] = int(db_stats["total"]) + 1
        if covered:
            db_stats["covered"] = int(db_stats["covered"]) + 1
        if covered and oracle_correct:
            db_stats["all_oracle_correct"] = int(db_stats["all_oracle_correct"]) + 1
        if current_correct:
            db_stats["current_correct"] = int(db_stats["current_correct"]) + 1
        db_stats[partition] = int(db_stats[partition]) + 1

        if partition != "both_correct" and len(examples) < sample_examples:
            examples.append(
                OracleGapExample(
                    index=index,
                    db_id=db_id,
                    question=str(current_rec.get("question") or ""),
                    partition=partition,
                    current_correct=current_correct,
                    oracle_correct=oracle_correct,
                    covered=covered,
                    current_sql=str(current_rec.get("pred_sql") or ""),
                    oracle_sql=str(oracle_rec.get("pred_sql") or ""),
                    gold_sql=str(current_rec.get("gold_sql") or ""),
                    slot_gap_bucket=str(slot_gap.get("bucket") or "not_applicable"),
                    slot_mismatches=int(slot_gap.get("mismatch_total") or 0),
                    slot_gold_missing=int(slot_gap.get("missing_total") or 0),
                    slot_pre_stage3_missing=int(
                        slot_gap.get("pre_stage3_missing_total") or 0
                    ),
                    slot_lost_before_scoring=int(
                        slot_gap.get("lost_before_scoring_total") or 0
                    ),
                    slot_present_misranked=int(
                        slot_gap.get("present_misranked_total") or 0
                    ),
                    slot_selected_binding_available=int(
                        slot_gap.get("selected_binding_available_total") or 0
                    ),
                    slot_selected_binding_missing=int(
                        slot_gap.get("selected_binding_missing_total") or 0
                    ),
                )
            )

    total = sum(partitions.values())
    covered_total = total - partitions["uncovered"]
    all_oracle_correct = sum(
        int(stats["all_oracle_correct"]) for stats in by_db.values()
    )
    live_recovered = partitions["both_correct"]
    for stats in by_db.values():
        denom = int(stats["all_oracle_correct"])
        stats["live_stage3_recovery"] = (
            int(stats["both_correct"]) / denom if denom else 0.0
        )

    return OracleGapReport(
        current_report=str(current_report_json),
        oracle_report=str(oracle_report_json),
        oracle_cache=str(oracle_cache_jsonl),
        previous_report=str(previous_report_json) if previous_report_json else None,
        total=total,
        oracle_cache_coverage=covered_total / total if total else 0.0,
        covered_total=covered_total,
        all_oracle_covered_correct=all_oracle_correct,
        all_oracle_covered_acc=all_oracle_correct / covered_total
        if covered_total
        else 0.0,
        live_stage3_recovered=live_recovered,
        live_stage3_recovery=live_recovered / all_oracle_correct
        if all_oracle_correct
        else 0.0,
        regressions_from_previous=regressions,
        partitions=partitions,
        by_db=dict(sorted(by_db.items())),
        slot_totals_by_kind=dict(sorted(slot_totals.items())),
        slot_mismatches_by_kind=dict(sorted(slot_mismatches.items())),
        slot_gold_missing_by_kind=dict(sorted(slot_gold_missing.items())),
        slot_pre_stage3_missing_by_kind=dict(sorted(slot_pre_stage3_missing.items())),
        slot_lost_before_scoring_by_kind=dict(sorted(slot_lost_before_scoring.items())),
        slot_present_misranked_by_kind=dict(sorted(slot_present_misranked.items())),
        slot_selected_binding_available_by_kind=dict(
            sorted(slot_selected_binding_available.items())
        ),
        slot_selected_binding_missing_by_kind=dict(
            sorted(slot_selected_binding_missing.items())
        ),
        oracle_only_triage=dict(sorted(oracle_only_triage.items())),
        examples=examples,
    )


def oracle_gap_report_to_json(report: OracleGapReport) -> str:
    return json.dumps(asdict(report), indent=2, sort_keys=True)


def render_oracle_gap_markdown(report: OracleGapReport) -> str:
    lines = [
        "# SemanticSQL Oracle Gap Report",
        "",
        f"- current: `{report.current_report}`",
        f"- oracle: `{report.oracle_report}`",
        f"- oracle_cache: `{report.oracle_cache}`",
        f"- total: `{report.total}`",
        f"- oracle_cache_coverage: `{report.covered_total}/{report.total} = {report.oracle_cache_coverage:.3%}`",
        f"- all_oracle_covered_acc: `{report.all_oracle_covered_correct}/{report.covered_total} = {report.all_oracle_covered_acc:.3%}`",
        f"- live_stage3_recovery: `{report.live_stage3_recovered}/{report.all_oracle_covered_correct} = {report.live_stage3_recovery:.3%}`",
        f"- regressions_from_previous: `{report.regressions_from_previous}`",
        "",
        "## Partitions",
        "",
    ]
    for name, count in report.partitions.items():
        lines.append(f"- `{name}`: {count}")
    lines.extend(["", "## By DB", "", "| db_id | covered | oracle_correct | current_correct | recovery |", "|---|---:|---:|---:|---:|"])
    for db_id, stats in report.by_db.items():
        lines.append(
            f"| `{db_id}` | {stats['covered']}/{stats['total']} "
            f"| {stats['all_oracle_correct']} | {stats['current_correct']} "
            f"| {float(stats['live_stage3_recovery']):.3%} |"
        )
    lines.extend(["", "## Slot Recovery Pressure", ""])
    if report.slot_totals_by_kind:
        for kind, total in report.slot_totals_by_kind.items():
            mismatch = report.slot_mismatches_by_kind.get(kind, 0)
            missing = report.slot_gold_missing_by_kind.get(kind, 0)
            present = report.slot_present_misranked_by_kind.get(kind, 0)
            lines.append(
                f"- `{kind}`: {mismatch}/{total} mismatched; "
                f"{missing} gold values absent from scored candidates; "
                f"{present} present but misranked"
            )
    else:
        lines.append("- `<none>`: 0")
    lines.extend(["", "## Oracle-Only Slot Triage", ""])
    for bucket, count in report.oracle_only_triage.items():
        lines.append(f"- `{bucket}`: {count}")
    if report.slot_pre_stage3_missing_by_kind or report.slot_lost_before_scoring_by_kind:
        lines.extend(["", "## Pre-Stage-3 Contract Pressure", ""])
        kinds = sorted(
            set(report.slot_pre_stage3_missing_by_kind)
            | set(report.slot_lost_before_scoring_by_kind)
        )
        for kind in kinds:
            pre_missing = report.slot_pre_stage3_missing_by_kind.get(kind, 0)
            lost = report.slot_lost_before_scoring_by_kind.get(kind, 0)
            lines.append(
                f"- `{kind}`: {pre_missing} absent before Stage 3; "
                f"{lost} present before Stage 3 but absent from scored candidates"
            )
    if (
        report.slot_selected_binding_available_by_kind
        or report.slot_selected_binding_missing_by_kind
    ):
        lines.extend(["", "## Selected-Field Frame Pressure", ""])
        kinds = sorted(
            set(report.slot_selected_binding_available_by_kind)
            | set(report.slot_selected_binding_missing_by_kind)
        )
        for kind in kinds:
            available = report.slot_selected_binding_available_by_kind.get(kind, 0)
            missing = report.slot_selected_binding_missing_by_kind.get(kind, 0)
            lines.append(
                f"- `{kind}`: {available} mismatched gold values present in "
                f"selected bindings; {missing} missing from selected bindings"
            )
    if report.examples:
        lines.extend(["", "## Sample Gaps", ""])
        for ex in report.examples:
            lines.extend(
                [
                    f"### {ex.index}: {ex.db_id}",
                    "",
                    f"- partition: `{ex.partition}`",
                    f"- covered: `{ex.covered}`",
                    f"- oracle_correct: `{ex.oracle_correct}`",
                    f"- current_correct: `{ex.current_correct}`",
                    f"- slot_gap_bucket: `{ex.slot_gap_bucket}`",
                    f"- slot_mismatches: `{ex.slot_mismatches}`",
                    f"- slot_gold_missing: `{ex.slot_gold_missing}`",
                    f"- slot_pre_stage3_missing: `{ex.slot_pre_stage3_missing}`",
                    f"- slot_lost_before_scoring: `{ex.slot_lost_before_scoring}`",
                    f"- slot_present_misranked: `{ex.slot_present_misranked}`",
                    f"- slot_selected_binding_available: `{ex.slot_selected_binding_available}`",
                    f"- slot_selected_binding_missing: `{ex.slot_selected_binding_missing}`",
                    f"- question: {ex.question}",
                    "",
                    "```sql",
                    "-- gold",
                    ex.gold_sql,
                    "-- oracle",
                    ex.oracle_sql,
                    "-- current",
                    ex.current_sql,
                    "```",
                    "",
                ]
            )
    return "\n".join(lines).rstrip() + "\n"


def canonicalize_slot_value(value: object, slot_name: str) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    text = str(value).strip()
    if slot_name.startswith("@field"):
        return _canonicalize_field_target(text)
    if slot_name.startswith("@entity"):
        return _canonicalize_name(text)
    if _is_quoted(text):
        return _normalise_quoted(text)
    return text.lower()


def _load_report(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected JSON object")
    return raw


def _records_by_key(report: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for rec in report.get("examples") or []:
        if isinstance(rec, dict):
            out[_example_key(rec)] = rec
    return out


def _load_oracle_cache(path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError(f"{path}:{line_no}: expected object")
        db_id = row.get("db_id")
        question = row.get("question") or row.get("nl")
        if isinstance(db_id, str) and isinstance(question, str):
            out[(db_id, question)] = row
    return out


def _example_key(rec: dict[str, Any]) -> tuple[str, str]:
    return (str(rec.get("db_id") or ""), str(rec.get("question") or ""))


def _slot_gap_stats(
    current_rec: dict[str, Any],
    cache_rec: dict[str, Any],
) -> dict[str, Any]:
    totals: dict[str, int] = {}
    mismatches: dict[str, int] = {}
    gold_missing: dict[str, int] = {}
    pre_stage3_missing: dict[str, int] = {}
    lost_before_scoring: dict[str, int] = {}
    present_misranked: dict[str, int] = {}
    selected_binding_available: dict[str, int] = {}
    selected_binding_missing: dict[str, int] = {}
    slot_map = cache_rec.get("slot_map")
    decisions = current_rec.get("stage3_slots")
    if not isinstance(slot_map, dict) or not isinstance(decisions, list) or not decisions:
        return {
            "bucket": "missing_stage3_trace",
            "totals_by_kind": totals,
            "mismatches_by_kind": mismatches,
            "gold_missing_by_kind": gold_missing,
            "pre_stage3_missing_by_kind": pre_stage3_missing,
            "lost_before_scoring_by_kind": lost_before_scoring,
            "present_misranked_by_kind": present_misranked,
            "selected_binding_available_by_kind": selected_binding_available,
            "selected_binding_missing_by_kind": selected_binding_missing,
            "mismatch_total": 0,
            "missing_total": 0,
            "pre_stage3_missing_total": 0,
            "lost_before_scoring_total": 0,
            "present_misranked_total": 0,
            "selected_binding_available_total": 0,
            "selected_binding_missing_total": 0,
        }
    pre_stage3_candidates = _pre_stage3_candidates_by_slot(current_rec)
    selected_bindings, has_selected_bindings = _selected_bindings_by_slot(current_rec)
    for decision in decisions:
        if not isinstance(decision, dict):
            continue
        slot_name = decision.get("slot_name")
        if not isinstance(slot_name, str) or slot_name not in slot_map:
            continue
        kind = str(decision.get("slot_kind") or "other")
        totals[kind] = totals.get(kind, 0) + 1
        expected = canonicalize_slot_value(slot_map[slot_name], slot_name)
        picked = canonicalize_slot_value(decision.get("picked"), slot_name)
        if expected == picked:
            continue
        mismatches[kind] = mismatches.get(kind, 0) + 1
        if kind == "value" and has_selected_bindings:
            if expected in selected_bindings.get(slot_name, set()):
                selected_binding_available[kind] = (
                    selected_binding_available.get(kind, 0) + 1
                )
            else:
                selected_binding_missing[kind] = (
                    selected_binding_missing.get(kind, 0) + 1
                )
        candidates = decision.get("candidates") or []
        candidate_values = []
        if isinstance(candidates, list):
            for candidate in candidates:
                if isinstance(candidate, dict):
                    candidate_values.append(
                        canonicalize_slot_value(candidate.get("value"), slot_name)
                    )
        if expected in candidate_values:
            present_misranked[kind] = present_misranked.get(kind, 0) + 1
        else:
            gold_missing[kind] = gold_missing.get(kind, 0) + 1
            pre_values = pre_stage3_candidates.get(slot_name)
            if pre_values is None:
                continue
            if expected in pre_values:
                lost_before_scoring[kind] = lost_before_scoring.get(kind, 0) + 1
            else:
                pre_stage3_missing[kind] = pre_stage3_missing.get(kind, 0) + 1
    mismatch_total = sum(mismatches.values())
    missing_total = sum(gold_missing.values())
    pre_stage3_missing_total = sum(pre_stage3_missing.values())
    lost_before_scoring_total = sum(lost_before_scoring.values())
    present_misranked_total = sum(present_misranked.values())
    selected_binding_available_total = sum(selected_binding_available.values())
    selected_binding_missing_total = sum(selected_binding_missing.values())
    if pre_stage3_missing_total:
        bucket = "gold_absent_from_pre_stage3"
    elif lost_before_scoring_total:
        bucket = "gold_lost_before_scoring"
    elif missing_total:
        bucket = "gold_absent_from_candidates"
    elif present_misranked_total:
        bucket = "present_but_misranked"
    else:
        bucket = "all_slots_match"
    return {
        "bucket": bucket,
        "totals_by_kind": totals,
        "mismatches_by_kind": mismatches,
        "gold_missing_by_kind": gold_missing,
        "pre_stage3_missing_by_kind": pre_stage3_missing,
        "lost_before_scoring_by_kind": lost_before_scoring,
        "present_misranked_by_kind": present_misranked,
        "selected_binding_available_by_kind": selected_binding_available,
        "selected_binding_missing_by_kind": selected_binding_missing,
        "mismatch_total": mismatch_total,
        "missing_total": missing_total,
        "pre_stage3_missing_total": pre_stage3_missing_total,
        "lost_before_scoring_total": lost_before_scoring_total,
        "present_misranked_total": present_misranked_total,
        "selected_binding_available_total": selected_binding_available_total,
        "selected_binding_missing_total": selected_binding_missing_total,
    }


def _pre_stage3_candidates_by_slot(
    current_rec: dict[str, Any],
) -> dict[str, list[str]]:
    query_frame = current_rec.get("query_frame")
    if not isinstance(query_frame, dict):
        return {}
    pre_stage3 = query_frame.get("pre_stage3")
    if not isinstance(pre_stage3, dict):
        return {}
    slots = pre_stage3.get("slots")
    if not isinstance(slots, list):
        return {}
    out: dict[str, list[str]] = {}
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        slot_name = slot.get("slot")
        if not isinstance(slot_name, str):
            continue
        candidates = slot.get("candidates")
        values: list[str] = []
        if isinstance(candidates, list):
            for candidate in candidates:
                if isinstance(candidate, dict):
                    values.append(
                        canonicalize_slot_value(candidate.get("value"), slot_name)
                    )
        out[slot_name] = values
    return out


def _selected_bindings_by_slot(
    current_rec: dict[str, Any],
) -> tuple[dict[str, set[str]], bool]:
    query_frame = current_rec.get("query_frame")
    if not isinstance(query_frame, dict):
        return {}, False
    stage3 = query_frame.get("stage3")
    if not isinstance(stage3, dict):
        return {}, False
    selected_bindings = stage3.get("selected_bindings")
    if not isinstance(selected_bindings, list):
        return {}, False

    out: dict[str, set[str]] = {}
    for binding in selected_bindings:
        if not isinstance(binding, dict):
            continue
        slot_name = binding.get("slot")
        if not isinstance(slot_name, str):
            continue
        out.setdefault(slot_name, set()).add(
            canonicalize_slot_value(binding.get("candidate_value"), slot_name)
        )
    return out, True


def _accumulate_slot_stats(
    slot_gap: dict[str, Any],
    totals: dict[str, int],
    mismatches: dict[str, int],
    gold_missing: dict[str, int],
    pre_stage3_missing: dict[str, int],
    lost_before_scoring: dict[str, int],
    present_misranked: dict[str, int],
    selected_binding_available: dict[str, int],
    selected_binding_missing: dict[str, int],
) -> None:
    for target, source_name in [
        (totals, "totals_by_kind"),
        (mismatches, "mismatches_by_kind"),
        (gold_missing, "gold_missing_by_kind"),
        (pre_stage3_missing, "pre_stage3_missing_by_kind"),
        (lost_before_scoring, "lost_before_scoring_by_kind"),
        (present_misranked, "present_misranked_by_kind"),
        (selected_binding_available, "selected_binding_available_by_kind"),
        (selected_binding_missing, "selected_binding_missing_by_kind"),
    ]:
        source = slot_gap.get(source_name)
        if not isinstance(source, dict):
            continue
        for kind, count in source.items():
            target[str(kind)] = target.get(str(kind), 0) + int(count)


_NON_CANONICAL_CHARS = re.compile(r"[^a-z0-9]+")


def _canonicalize_field_target(target: str) -> str:
    if "." not in target:
        return _canonicalize_name(target)
    entity, field = target.split(".", 1)
    return f"{_canonicalize_name(entity)}.{_canonicalize_name(field)}"


def _canonicalize_name(raw: str) -> str:
    stripped = raw.strip().strip("`").strip('"')
    canonical = _NON_CANONICAL_CHARS.sub("_", stripped.lower()).strip("_")
    canonical = re.sub(r"_+", "_", canonical)
    if not canonical:
        return "_"
    if canonical[0].isdigit():
        canonical = f"_{canonical}"
    return canonical


def _is_quoted(text: str) -> bool:
    return len(text) >= 2 and text[0] == "'" and text[-1] == "'"


def _normalise_quoted(text: str) -> str:
    inner = text[1:-1].replace("''", "'").strip().lower()
    return f"'{inner}'"
