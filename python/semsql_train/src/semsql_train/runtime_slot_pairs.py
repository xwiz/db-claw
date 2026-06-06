"""Derive Stage 3 training pairs from runtime slot traces.

The v3 teacher-cache generator builds static candidate sets. This module
instead consumes `python -m semsql_eval spider --report-json` output with
`stage3_slots` traces, so the resulting examples mirror the runtime
candidate order, field/value filtering, and hard negatives that actually
reached the cross-encoder.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .generators_slot_v3 import _canonicalize_slot_value

__all__ = [
    "RuntimeSlotDeriveStats",
    "derive_runtime_slot_pairs",
    "load_oracle_slot_maps",
    "write_runtime_slot_pairs",
]


@dataclass(frozen=True)
class RuntimeSlotDeriveStats:
    """Counters from one runtime-slot derivation run."""

    reports: int = 0
    examples: int = 0
    examples_with_oracle: int = 0
    slots_seen: int = 0
    pairs_written: int = 0
    gold_appended: int = 0
    missing_oracle: int = 0
    missing_trace: int = 0


def load_oracle_slot_maps(path: Path) -> dict[tuple[str, str], dict[str, str]]:
    """Load `(db_id, nl) -> canonical slot_map` from a teacher-cache JSONL."""
    out: dict[tuple[str, str], dict[str, str]] = {}
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        text = line.strip()
        if not text:
            continue
        row = json.loads(text)
        if not isinstance(row, dict):
            continue
        db_id = row.get("db_id")
        nl = row.get("nl") or row.get("question")
        slot_map = row.get("slot_map")
        if not isinstance(db_id, str) or not isinstance(nl, str):
            continue
        if not isinstance(slot_map, dict):
            continue
        canonical: dict[str, str] = {}
        for slot, value in slot_map.items():
            if isinstance(slot, str) and isinstance(value, str):
                canonical[slot] = _canonicalize_slot_value(slot, value)
        if canonical:
            out[(db_id, nl)] = canonical
    return out


def derive_runtime_slot_pairs(
    report_json: Path,
    oracle_slot_maps: dict[tuple[str, str], dict[str, str]],
    *,
    append_missing_gold: bool = True,
    max_candidates: int | None = None,
    context_mode: str = "actual",
) -> tuple[list[dict[str, Any]], RuntimeSlotDeriveStats]:
    """Return Stage 3 rows derived from one eval report's runtime traces."""
    if context_mode not in ("actual", "teacher-forced", "both"):
        raise ValueError(f"unsupported context_mode: {context_mode}")
    report = json.loads(report_json.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    examples = report.get("examples") or []
    stats = RuntimeSlotDeriveStats(reports=1, examples=len(examples))
    counters = stats.__dict__.copy()
    for example in examples:
        if not isinstance(example, dict):
            continue
        db_id = example.get("db_id")
        nl = example.get("question")
        if not isinstance(db_id, str) or not isinstance(nl, str):
            continue
        slot_map = oracle_slot_maps.get((db_id, nl))
        if slot_map is None:
            counters["missing_oracle"] += 1
            continue
        counters["examples_with_oracle"] += 1
        traces = example.get("stage3_slots") or []
        if not isinstance(traces, list) or not traces:
            counters["missing_trace"] += 1
            continue
        original_skeleton: str | None = None
        previous_gold: dict[str, str] = {}
        for trace in traces:
            if not isinstance(trace, dict):
                continue
            slot = trace.get("slot_name")
            gold = slot_map.get(slot) if isinstance(slot, str) else None
            if not isinstance(slot, str) or not isinstance(gold, str):
                continue
            actual_skeleton = trace.get("context_skeleton") or trace.get("skeleton")
            if not isinstance(actual_skeleton, str) or not actual_skeleton:
                continue
            if original_skeleton is None:
                original_skeleton = actual_skeleton
            candidates = _trace_candidates(trace)
            if not candidates:
                continue
            counters["slots_seen"] += 1
            if gold not in candidates:
                if not append_missing_gold:
                    continue
                candidates.append(gold)
                counters["gold_appended"] += 1
            if max_candidates is not None and len(candidates) > max_candidates:
                candidates = _trim_candidates(candidates, gold, max_candidates)
            correct_index = candidates.index(gold)
            for variant_mode, skeleton in _context_variants(
                context_mode,
                actual_skeleton=actual_skeleton,
                original_skeleton=original_skeleton,
                previous_gold=previous_gold,
            ):
                rows.append(
                    {
                        "stage": 3,
                        "nl": nl,
                        "skeleton": skeleton,
                        "slot_name": slot,
                        "candidates": candidates,
                        "correct_index": correct_index,
                        "runtime_trace": True,
                        "runtime_context_mode": variant_mode,
                        "slot_kind": trace.get("slot_kind"),
                        "predicate_field": trace.get("predicate_field"),
                    }
                )
                counters["pairs_written"] += 1
            previous_gold[slot] = gold
    return rows, RuntimeSlotDeriveStats(**counters)


def write_runtime_slot_pairs(
    report_json: Path,
    oracle_cache: Path,
    out_jsonl: Path,
    *,
    append_missing_gold: bool = True,
    max_candidates: int | None = None,
    context_mode: str = "actual",
) -> RuntimeSlotDeriveStats:
    """Write runtime-trace Stage 3 pairs to JSONL and return counters."""
    oracle = load_oracle_slot_maps(oracle_cache)
    rows, stats = derive_runtime_slot_pairs(
        report_json,
        oracle,
        append_missing_gold=append_missing_gold,
        max_candidates=max_candidates,
        context_mode=context_mode,
    )
    out_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with out_jsonl.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True))
            fh.write("\n")
    return stats


def _trace_candidates(trace: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for item in trace.get("candidates") or []:
        if not isinstance(item, dict):
            continue
        value = item.get("value")
        if isinstance(value, str) and value not in out:
            out.append(_canonicalize_slot_value(str(trace.get("slot_name", "")), value))
    return out


def _trim_candidates(candidates: list[str], gold: str, max_candidates: int) -> list[str]:
    if max_candidates < 1:
        return [gold]
    kept = candidates[:max_candidates]
    if gold not in kept:
        kept[-1] = gold
    return kept


def _context_variants(
    context_mode: str,
    *,
    actual_skeleton: str,
    original_skeleton: str,
    previous_gold: dict[str, str],
) -> list[tuple[str, str]]:
    teacher_forced = _apply_previous_gold(original_skeleton, previous_gold)
    if context_mode == "actual":
        return [("actual", actual_skeleton)]
    if context_mode == "teacher-forced":
        return [("teacher_forced", teacher_forced)]
    variants = [("teacher_forced", teacher_forced)]
    if actual_skeleton != teacher_forced:
        variants.append(("actual", actual_skeleton))
    return variants


def _apply_previous_gold(skeleton: str, previous_gold: dict[str, str]) -> str:
    out = skeleton
    for slot, gold in sorted(previous_gold.items(), key=lambda item: len(item[0]), reverse=True):
        out = re.sub(re.escape(slot) + r"(?!\d)", gold, out)
    return out
