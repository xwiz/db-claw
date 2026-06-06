"""Offline corpus checks to run before expensive cascade retraining.

The normal trainer preflights validate shape, but they intentionally do
not know about benchmark profiles. This module catches the mistakes that
make a multi-day run look successful while training/evaluating against
the wrong contract:

* Stage 2 eval corpora with stale single-FROM targets for BIRD.
* Training corpora that accidentally include benchmark-dev questions.
* Stage 1 eval corpora whose per-question candidate groups are too tiny
  for recall@k to mean anything.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "AuditReport",
    "DatasetStats",
    "audit_corpora",
    "audit_report_to_json",
]

_BAD_PLACEHOLDER_RX = re.compile(r"@(?!entity\d+|field\d+|val\d+)\w*")
_CONCRETE_DOTTED_RX = re.compile(r"\b[a-zA-Z_][\w]*\.[a-zA-Z_][\w]*\b")
_BIRD_STAGE2_MIN_JOIN_RATE = 0.50


@dataclass(frozen=True)
class DatasetStats:
    """Small profile of one JSONL dataset."""

    path: str
    rows: int = 0
    join_rate: float = 0.0
    where_rate: float = 0.0
    order_rate: float = 0.0
    limit_rate: float = 0.0
    avg_target_tokens: float = 0.0
    malformed_placeholder_rows: int = 0
    concrete_identifier_rows: int = 0
    question_overlap_rows: int = 0
    linker_questions: int = 0
    linker_group_p50: int = 0
    linker_group_p90: int = 0
    linker_group_max: int = 0


@dataclass(frozen=True)
class AuditReport:
    """Audit output; `errors` must be empty before retraining."""

    profile: str
    datasets: list[DatasetStats] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def audit_report_to_json(report: AuditReport) -> str:
    """Serialize an [`AuditReport`] as stable pretty JSON."""

    return json.dumps(asdict(report), indent=2, sort_keys=True)


def audit_corpora(
    *,
    skeleton_train: Path | None = None,
    skeleton_eval: Path | None = None,
    linker_eval: Path | None = None,
    benchmark_questions: Path | None = None,
    profile: str = "generic",
    recall_k: int = 5,
) -> AuditReport:
    """Run offline checks across the corpora selected for a retrain."""

    benchmark_q = _load_benchmark_questions(benchmark_questions)
    datasets: list[DatasetStats] = []
    errors: list[str] = []
    warnings: list[str] = []

    train_stats: DatasetStats | None = None
    eval_stats: DatasetStats | None = None
    if skeleton_train is not None:
        train_stats = _skeleton_stats(skeleton_train, benchmark_q)
        datasets.append(train_stats)
        _collect_skeleton_contract_issues(train_stats, "train", errors)
        if train_stats.question_overlap_rows:
            errors.append(
                f"skeleton train overlaps benchmark questions: "
                f"{train_stats.question_overlap_rows} row(s)"
            )

    if skeleton_eval is not None:
        eval_stats = _skeleton_stats(skeleton_eval, set())
        datasets.append(eval_stats)
        _collect_skeleton_contract_issues(eval_stats, "eval", errors)

    if train_stats is not None and eval_stats is not None:
        if eval_stats.join_rate + 0.25 < train_stats.join_rate:
            errors.append(
                "skeleton eval JOIN coverage is far below train coverage: "
                f"eval={eval_stats.join_rate:.1%}, train={train_stats.join_rate:.1%}"
            )
        if profile == "bird-stage2" and eval_stats.join_rate < _BIRD_STAGE2_MIN_JOIN_RATE:
            errors.append(
                "BIRD Stage 2 eval JOIN coverage is too low: "
                f"{eval_stats.join_rate:.1%} < {_BIRD_STAGE2_MIN_JOIN_RATE:.0%}. "
                "This usually means a stale single-FROM BIRD eval cache."
            )

    if linker_eval is not None:
        stats = _linker_stats(linker_eval)
        datasets.append(stats)
        if stats.linker_questions and stats.linker_group_p50 <= recall_k:
            warnings.append(
                f"linker eval median candidate group size is {stats.linker_group_p50}, "
                f"which is <= recall@{recall_k}; recall can be trivial. "
                "Rebuild eval with full per-question candidate pools."
            )
        if stats.linker_questions and stats.linker_group_p90 <= recall_k:
            errors.append(
                f"linker eval p90 candidate group size is {stats.linker_group_p90}, "
                f"which is <= recall@{recall_k}; this cannot validate ranking."
            )

    return AuditReport(
        profile=profile,
        datasets=datasets,
        errors=errors,
        warnings=warnings,
    )


def _collect_skeleton_contract_issues(
    stats: DatasetStats,
    label: str,
    errors: list[str],
) -> None:
    if stats.rows == 0:
        errors.append(f"skeleton {label} has no rows: {stats.path}")
    if stats.malformed_placeholder_rows:
        errors.append(
            f"skeleton {label} has malformed placeholders in "
            f"{stats.malformed_placeholder_rows} row(s)"
        )
    if stats.concrete_identifier_rows:
        errors.append(
            f"skeleton {label} has concrete dotted identifiers in "
            f"{stats.concrete_identifier_rows} row(s); expected placeholder skeletons"
        )


def _skeleton_stats(path: Path, benchmark_questions: set[str]) -> DatasetStats:
    rows = 0
    join = where = order = limit = 0
    token_total = 0
    bad_placeholder = 0
    concrete = 0
    overlap = 0
    for rec in _read_jsonl(path):
        rows += 1
        target = str(rec.get("natsql_skeleton") or "")
        upper = target.upper()
        join += int(" JOIN " in upper)
        where += int(" WHERE " in upper)
        order += int(" ORDER BY " in upper)
        limit += int(" LIMIT " in upper)
        token_total += len(target.split())
        bad_placeholder += int(_BAD_PLACEHOLDER_RX.search(target) is not None)
        concrete += int(_CONCRETE_DOTTED_RX.search(target) is not None)
        if _normalise_question(str(rec.get("nl") or "")) in benchmark_questions:
            overlap += 1

    return DatasetStats(
        path=str(path),
        rows=rows,
        join_rate=_rate(join, rows),
        where_rate=_rate(where, rows),
        order_rate=_rate(order, rows),
        limit_rate=_rate(limit, rows),
        avg_target_tokens=(token_total / rows) if rows else 0.0,
        malformed_placeholder_rows=bad_placeholder,
        concrete_identifier_rows=concrete,
        question_overlap_rows=overlap,
    )


def _linker_stats(path: Path) -> DatasetStats:
    rows = 0
    grouped: dict[str, int] = defaultdict(int)
    for rec in _read_jsonl(path):
        rows += 1
        qid = str(rec.get("nl") or rec.get("question") or "")
        grouped[qid] += 1
    sizes = sorted(grouped.values())
    return DatasetStats(
        path=str(path),
        rows=rows,
        linker_questions=len(grouped),
        linker_group_p50=_percentile_int(sizes, 0.50),
        linker_group_p90=_percentile_int(sizes, 0.90),
        linker_group_max=sizes[-1] if sizes else 0,
    )


def _load_benchmark_questions(path: Path | None) -> set[str]:
    if path is None:
        return set()
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        return set()
    out: set[str] = set()
    for row in raw:
        if not isinstance(row, dict):
            continue
        q = row.get("question") or row.get("Question")
        if isinstance(q, str):
            out.add(_normalise_question(q))
    return out


def _read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            if isinstance(row, dict):
                yield row


def _normalise_question(question: str) -> str:
    return " ".join(question.strip().lower().split())


def _rate(count: int, rows: int) -> float:
    return (count / rows) if rows else 0.0


def _percentile_int(values: list[int], pct: float) -> int:
    if not values:
        return 0
    idx = min(len(values) - 1, max(0, int(len(values) * pct)))
    return values[idx]
