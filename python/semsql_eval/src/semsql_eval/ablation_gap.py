"""Live-path oracle ablation diagnostics.

This report compares the same examples across a fully live run and one or
more oracle-ablation runs. It is intentionally narrower than SQL feature
diagnostics: its job is to answer which upstream live stage is blocking an
example before we chase another Stage 3/value patch.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

AccuracyStats = dict[str, int | float | None]

__all__ = [
    "AblationGapExample",
    "AblationGapReport",
    "ablation_gap_report",
    "ablation_gap_report_to_json",
    "render_ablation_gap_markdown",
]


@dataclass(frozen=True)
class AblationGapExample:
    index: int
    db_id: str
    question: str
    partition: str
    live_correct: bool
    oracle_schema_correct: bool | None
    oracle_stage2_correct: bool | None
    oracle_schema_stage2_correct: bool | None
    all_oracle_correct: bool | None
    live_sql: str
    oracle_schema_sql: str
    oracle_stage2_sql: str
    oracle_schema_stage2_sql: str
    all_oracle_sql: str
    gold_sql: str


@dataclass(frozen=True)
class AblationGapReport:
    live_report: str
    oracle_schema_report: str | None
    oracle_stage2_report: str | None
    oracle_schema_stage2_report: str | None
    all_oracle_report: str | None
    total: int
    accuracies: dict[str, AccuracyStats]
    partitions: dict[str, int]
    by_db: dict[str, dict[str, int | float]]
    examples: list[AblationGapExample] = field(default_factory=list)


def ablation_gap_report(
    *,
    live_report_json: Path,
    oracle_schema_report_json: Path | None = None,
    oracle_stage2_report_json: Path | None = None,
    oracle_schema_stage2_report_json: Path | None = None,
    all_oracle_report_json: Path | None = None,
    sample_examples: int = 20,
) -> AblationGapReport:
    live = _load_report(live_report_json)
    oracle_schema = _optional_records(oracle_schema_report_json)
    oracle_stage2 = _optional_records(oracle_stage2_report_json)
    oracle_schema_stage2 = _optional_records(oracle_schema_stage2_report_json)
    all_oracle = _optional_records(all_oracle_report_json)

    partitions = {
        "live_correct": 0,
        "schema_linking_blocked": 0,
        "stage2_shape_blocked": 0,
        "schema_and_stage2_coupled": 0,
        "both_single_oracles_recover": 0,
        "stage3_or_renderer_blocked": 0,
        "oracle_ceiling_or_dataset": 0,
        "unrecovered_by_ablation": 0,
    }
    by_db: dict[str, dict[str, int | float]] = {}
    examples: list[AblationGapExample] = []

    live_records = live.get("examples") or []
    if not isinstance(live_records, list):
        raise ValueError(f"{live_report_json}: expected examples to be a list")
    live_keys = [
        _example_key(rec)
        for rec in live_records
        if isinstance(rec, dict)
    ]

    for idx, live_rec in enumerate(live_records):
        if not isinstance(live_rec, dict):
            continue
        key = _example_key(live_rec)
        schema_rec = oracle_schema.get(key, {}) if oracle_schema is not None else {}
        stage2_rec = oracle_stage2.get(key, {}) if oracle_stage2 is not None else {}
        both_rec = (
            oracle_schema_stage2.get(key, {})
            if oracle_schema_stage2 is not None
            else {}
        )
        all_rec = all_oracle.get(key, {}) if all_oracle is not None else {}

        live_correct = _is_correct(live_rec)
        schema_correct = _optional_correct(schema_rec, oracle_schema)
        stage2_correct = _optional_correct(stage2_rec, oracle_stage2)
        both_correct = _optional_correct(both_rec, oracle_schema_stage2)
        all_correct = _optional_correct(all_rec, all_oracle)
        partition = _partition(
            live_correct=live_correct,
            oracle_schema_correct=schema_correct,
            oracle_stage2_correct=stage2_correct,
            oracle_schema_stage2_correct=both_correct,
            all_oracle_correct=all_correct,
        )
        partitions[partition] = partitions.get(partition, 0) + 1

        db_id = str(live_rec.get("db_id") or "")
        db_stats = by_db.setdefault(
            db_id,
            {
                "total": 0,
                "live_correct": 0,
                "oracle_schema_correct": 0,
                "oracle_stage2_correct": 0,
                "oracle_schema_stage2_correct": 0,
                "all_oracle_correct": 0,
                "live_acc": 0.0,
                "oracle_schema_stage2_acc": 0.0,
            },
        )
        db_stats["total"] = int(db_stats["total"]) + 1
        if live_correct:
            db_stats["live_correct"] = int(db_stats["live_correct"]) + 1
        if schema_correct:
            db_stats["oracle_schema_correct"] = int(db_stats["oracle_schema_correct"]) + 1
        if stage2_correct:
            db_stats["oracle_stage2_correct"] = int(db_stats["oracle_stage2_correct"]) + 1
        if both_correct:
            db_stats["oracle_schema_stage2_correct"] = (
                int(db_stats["oracle_schema_stage2_correct"]) + 1
            )
        if all_correct:
            db_stats["all_oracle_correct"] = int(db_stats["all_oracle_correct"]) + 1

        if partition != "live_correct" and len(examples) < sample_examples:
            examples.append(
                AblationGapExample(
                    index=idx,
                    db_id=db_id,
                    question=str(live_rec.get("question") or ""),
                    partition=partition,
                    live_correct=live_correct,
                    oracle_schema_correct=schema_correct,
                    oracle_stage2_correct=stage2_correct,
                    oracle_schema_stage2_correct=both_correct,
                    all_oracle_correct=all_correct,
                    live_sql=str(live_rec.get("pred_sql") or ""),
                    oracle_schema_sql=str(schema_rec.get("pred_sql") or ""),
                    oracle_stage2_sql=str(stage2_rec.get("pred_sql") or ""),
                    oracle_schema_stage2_sql=str(both_rec.get("pred_sql") or ""),
                    all_oracle_sql=str(all_rec.get("pred_sql") or ""),
                    gold_sql=str(live_rec.get("gold_sql") or ""),
                )
            )

    total = sum(partitions.values())
    for stats in by_db.values():
        db_total = int(stats["total"])
        if db_total:
            stats["live_acc"] = int(stats["live_correct"]) / db_total
            stats["oracle_schema_stage2_acc"] = (
                int(stats["oracle_schema_stage2_correct"]) / db_total
            )

    return AblationGapReport(
        live_report=str(live_report_json),
        oracle_schema_report=str(oracle_schema_report_json)
        if oracle_schema_report_json
        else None,
        oracle_stage2_report=str(oracle_stage2_report_json)
        if oracle_stage2_report_json
        else None,
        oracle_schema_stage2_report=str(oracle_schema_stage2_report_json)
        if oracle_schema_stage2_report_json
        else None,
        all_oracle_report=str(all_oracle_report_json) if all_oracle_report_json else None,
        total=total,
        accuracies={
            "live": _accuracy(live_records),
            "oracle_schema": _accuracy_for_keys(oracle_schema, live_keys)
            if oracle_schema is not None
            else _missing_accuracy(),
            "oracle_stage2": _accuracy_for_keys(oracle_stage2, live_keys)
            if oracle_stage2 is not None
            else _missing_accuracy(),
            "oracle_schema_stage2": _accuracy_for_keys(oracle_schema_stage2, live_keys)
            if oracle_schema_stage2 is not None
            else _missing_accuracy(),
            "all_oracle": _accuracy_for_keys(all_oracle, live_keys)
            if all_oracle is not None
            else _missing_accuracy(),
        },
        partitions=partitions,
        by_db=dict(sorted(by_db.items())),
        examples=examples,
    )


def ablation_gap_report_to_json(report: AblationGapReport) -> str:
    return json.dumps(asdict(report), indent=2, sort_keys=True)


def render_ablation_gap_markdown(report: AblationGapReport) -> str:
    lines = [
        "# SemanticSQL Live-Path Ablation Gap",
        "",
        f"- live: `{report.live_report}`",
    ]
    if report.oracle_schema_report:
        lines.append(f"- oracle_schema: `{report.oracle_schema_report}`")
    if report.oracle_stage2_report:
        lines.append(f"- oracle_stage2: `{report.oracle_stage2_report}`")
    if report.oracle_schema_stage2_report:
        lines.append(
            f"- oracle_schema_stage2: `{report.oracle_schema_stage2_report}`"
        )
    if report.all_oracle_report:
        lines.append(f"- all_oracle: `{report.all_oracle_report}`")
    lines.extend(["", "## Accuracy Ladder", ""])
    for name, acc_stats in report.accuracies.items():
        correct = acc_stats.get("correct")
        total = acc_stats.get("total")
        acc = acc_stats.get("acc")
        if correct is None or total is None or acc is None:
            lines.append(f"- `{name}`: `<not provided>`")
        else:
            lines.append(f"- `{name}`: `{correct}/{total} = {float(acc):.3%}`")
    lines.extend(["", "## Partitions", ""])
    for name, count in report.partitions.items():
        lines.append(f"- `{name}`: {count}")
    lines.extend(["", "## By DB", ""])
    if report.by_db:
        lines.append("| db_id | live | oracle_schema_stage2 |")
        lines.append("|---|---:|---:|")
        for db_id, db_stats in report.by_db.items():
            total = int(db_stats["total"])
            live_correct = int(db_stats["live_correct"])
            both_correct = int(db_stats["oracle_schema_stage2_correct"])
            lines.append(f"| `{db_id}` | {live_correct}/{total} | {both_correct}/{total} |")
    else:
        lines.append("_No DB rows._")
    if report.examples:
        lines.extend(["", "## Sample Gaps", ""])
        for example in report.examples:
            lines.extend(
                [
                    f"### {example.index}: {example.db_id}",
                    "",
                    f"- partition: `{example.partition}`",
                    f"- live_correct: `{example.live_correct}`",
                    f"- oracle_schema_correct: `{example.oracle_schema_correct}`",
                    f"- oracle_stage2_correct: `{example.oracle_stage2_correct}`",
                    f"- oracle_schema_stage2_correct: `{example.oracle_schema_stage2_correct}`",
                    f"- all_oracle_correct: `{example.all_oracle_correct}`",
                    f"- question: {example.question}",
                    "",
                    "```sql",
                    "-- gold",
                    example.gold_sql,
                    "-- live",
                    example.live_sql,
                ]
            )
            if example.oracle_schema_sql:
                lines.extend(["-- oracle schema", example.oracle_schema_sql])
            if example.oracle_stage2_sql:
                lines.extend(["-- oracle Stage 2", example.oracle_stage2_sql])
            if example.oracle_schema_stage2_sql:
                lines.extend(["-- oracle schema + Stage 2", example.oracle_schema_stage2_sql])
            if example.all_oracle_sql:
                lines.extend(["-- all oracle", example.all_oracle_sql])
            lines.extend(["```", ""])
    return "\n".join(lines)


def _partition(
    *,
    live_correct: bool,
    oracle_schema_correct: bool | None,
    oracle_stage2_correct: bool | None,
    oracle_schema_stage2_correct: bool | None,
    all_oracle_correct: bool | None,
) -> str:
    if live_correct:
        return "live_correct"
    if oracle_schema_correct and oracle_stage2_correct:
        return "both_single_oracles_recover"
    if oracle_schema_correct:
        return "schema_linking_blocked"
    if oracle_stage2_correct:
        return "stage2_shape_blocked"
    if oracle_schema_stage2_correct:
        return "schema_and_stage2_coupled"
    if all_oracle_correct:
        return "stage3_or_renderer_blocked"
    if all_oracle_correct is False:
        return "oracle_ceiling_or_dataset"
    return "unrecovered_by_ablation"


def _load_report(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected report object")
    return raw


def _optional_records(path: Path | None) -> dict[tuple[str, str], dict[str, Any]] | None:
    if path is None:
        return None
    return _records_by_key(_load_report(path))


def _records_by_key(report: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    records: dict[tuple[str, str], dict[str, Any]] = {}
    for rec in report.get("examples") or []:
        if isinstance(rec, dict):
            records[_example_key(rec)] = rec
    return records


def _example_key(rec: dict[str, Any]) -> tuple[str, str]:
    return (str(rec.get("db_id") or ""), str(rec.get("question") or ""))


def _is_correct(rec: dict[str, Any]) -> bool:
    return bool(rec.get("exec_equal"))


def _optional_correct(
    rec: dict[str, Any],
    records: dict[tuple[str, str], dict[str, Any]] | None,
) -> bool | None:
    if records is None:
        return None
    return _is_correct(rec)


def _accuracy(records: list[Any]) -> AccuracyStats:
    dict_records = [rec for rec in records if isinstance(rec, dict)]
    total = len(dict_records)
    correct = sum(1 for rec in dict_records if rec.get("exec_equal"))
    return {
        "total": total,
        "correct": correct,
        "acc": correct / total if total else 0.0,
    }


def _accuracy_for_keys(
    records: dict[tuple[str, str], dict[str, Any]],
    keys: list[tuple[str, str]],
) -> AccuracyStats:
    total = len(keys)
    correct = sum(1 for key in keys if records.get(key, {}).get("exec_equal"))
    return {
        "total": total,
        "correct": correct,
        "acc": correct / total if total else 0.0,
    }


def _missing_accuracy() -> AccuracyStats:
    return {"total": None, "correct": None, "acc": None}
