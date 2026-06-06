"""Checkpoint-level per-stage evaluation helpers.

`semsql_eval.per_stage` contains pure metrics. This module bridges those
metrics to actual Hugging Face checkpoints so a training run can publish
Stage 1/2/3 numbers before the more expensive end-to-end BIRD gate.

ML imports are intentionally lazy; importing `semsql_train` should still
work on a non-ML install.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, TypeVar

__all__ = [
    "StageEvalReport",
    "eval_linker_checkpoint",
    "eval_skeleton_checkpoint",
    "eval_slot_checkpoint",
    "stage_eval_report_to_json",
]

_T = TypeVar("_T")
_RUNTIME_TOP_K_ENTITIES = 3
_RUNTIME_TOP_K_FIELDS = 7


@dataclass(frozen=True)
class StageEvalReport:
    """Serializable report for one stage checkpoint."""

    stage: str
    checkpoint: str
    eval_jsonl: str
    total: int
    metrics: dict[str, float]
    examples: list[dict[str, Any]] = field(default_factory=list)

    @property
    def primary_metric(self) -> float:
        """Best single-number metric for this stage."""

        for key in ("exact_match", "recall_at_k", "top1_accuracy"):
            if key in self.metrics:
                return self.metrics[key]
        return 0.0


def stage_eval_report_to_json(report: StageEvalReport) -> str:
    return json.dumps(asdict(report), indent=2, sort_keys=True)


def eval_skeleton_checkpoint(
    *,
    checkpoint: Path,
    eval_jsonl: Path,
    limit: int | None = None,
    batch_size: int = 8,
    max_new_tokens: int = 96,
    device: str | None = None,
    sample_examples: int = 20,
) -> StageEvalReport:
    """Generate skeletons from a seq2seq checkpoint and compute exact match."""

    try:
        import torch
        import transformers
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("Stage 2 eval requires `pip install semsql-train[ml]`.") from e

    from .trainers.skeleton import _format_source

    records = _take(_read_jsonl(eval_jsonl), limit)
    tokenizer = transformers.AutoTokenizer.from_pretrained(checkpoint)  # type: ignore[no-untyped-call]
    model = transformers.AutoModelForSeq2SeqLM.from_pretrained(checkpoint)
    dev = _resolve_device(torch, device)
    model.to(dev)
    model.eval()

    total = 0
    correct = 0
    examples: list[dict[str, Any]] = []
    with torch.no_grad():
        for batch in _batched(records, batch_size):
            sources = [_format_source(rec) for rec in batch]
            golds = [str(rec["natsql_skeleton"]) for rec in batch]
            enc = tokenizer(
                sources,
                max_length=256,
                truncation=True,
                padding=True,
                return_tensors="pt",
            )
            enc = {k: v.to(dev) for k, v in enc.items()}
            out = model.generate(
                **enc,
                max_new_tokens=max_new_tokens,
                num_beams=1,
            )
            preds = tokenizer.batch_decode(out, skip_special_tokens=True)
            for rec, pred, gold in zip(batch, preds, golds, strict=True):
                is_correct = _norm_sqlish(pred) == _norm_sqlish(gold)
                total += 1
                correct += int(is_correct)
                if len(examples) < sample_examples and not is_correct:
                    examples.append(
                        {
                            "nl": rec.get("nl"),
                            "gold": gold,
                            "pred": pred,
                            "exact": False,
                        }
                    )

    return StageEvalReport(
        stage="skeleton",
        checkpoint=str(checkpoint),
        eval_jsonl=str(eval_jsonl),
        total=total,
        metrics={"exact_match": _rate(correct, total)},
        examples=examples,
    )


def eval_linker_checkpoint(
    *,
    checkpoint: Path,
    eval_jsonl: Path,
    k: int = 5,
    limit: int | None = None,
    batch_size: int = 64,
    device: str | None = None,
    sample_examples: int = 20,
) -> StageEvalReport:
    """Score Stage 1 candidate rows and compute recall@k/nDCG@k."""

    rows = _read_linker_groups(eval_jsonl, limit)
    scored = _score_classifier_rows(
        checkpoint=checkpoint,
        rows=rows,
        text_pair_fn=lambda rec: (
            str(rec["nl"]),
            _format_linker_candidate(rec),
        ),
        batch_size=batch_size,
        device=device,
    )
    metrics, failures = _linker_metrics(scored, k=k, sample_examples=sample_examples)
    return StageEvalReport(
        stage="linker",
        checkpoint=str(checkpoint),
        eval_jsonl=str(eval_jsonl),
        total=len(scored),
        metrics=metrics,
        examples=failures,
    )


def eval_slot_checkpoint(
    *,
    checkpoint: Path,
    eval_jsonl: Path,
    limit: int | None = None,
    batch_size: int = 64,
    device: str | None = None,
    sample_examples: int = 20,
) -> StageEvalReport:
    """Score Stage 3 candidate sets and compute per-slot top-1 accuracy."""

    records = _take(_read_jsonl(eval_jsonl), limit)
    flat: list[dict[str, Any]] = []
    offsets: list[tuple[dict[str, Any], int, int]] = []
    for rec in records:
        candidates = rec.get("candidates")
        correct_index = rec.get("correct_index")
        if not isinstance(candidates, list) or not isinstance(correct_index, int):
            continue
        start = len(flat)
        for cand in candidates:
            flat.append({"record": rec, "candidate": str(cand)})
        offsets.append((rec, start, len(flat)))

    scored = _score_classifier_rows(
        checkpoint=checkpoint,
        rows=flat,
        text_pair_fn=lambda row: (
            str(row["record"]["nl"]),
            (
                f"slot {row['record']['slot_name']} in "
                f"[{row['record']['skeleton']}]: {row['candidate']}"
            ),
        ),
        batch_size=batch_size,
        device=device,
    )

    predictions: list[dict[str, Any]] = []
    for rec, start, end in offsets:
        cands = [str(c) for c in rec["candidates"]]
        scores = [scored[i]["score"] for i in range(start, end)]
        pred_idx = max(range(len(scores)), key=lambda i: scores[i]) if scores else -1
        correct_idx = int(rec["correct_index"])
        predictions.append(
            {
                "nl": rec["nl"],
                "slot_name": rec["slot_name"],
                "skeleton": rec["skeleton"],
                "candidates": cands,
                "correct_index": correct_idx,
                "pred_index": pred_idx,
                "gold": cands[correct_idx] if 0 <= correct_idx < len(cands) else None,
                "pred": cands[pred_idx] if 0 <= pred_idx < len(cands) else None,
            }
        )

    metrics, failures = _slot_metrics(predictions, sample_examples=sample_examples)
    return StageEvalReport(
        stage="slot-filler",
        checkpoint=str(checkpoint),
        eval_jsonl=str(eval_jsonl),
        total=len(predictions),
        metrics=metrics,
        examples=failures,
    )


def _score_classifier_rows(
    *,
    checkpoint: Path,
    rows: Sequence[dict[str, Any]],
    text_pair_fn: Callable[[dict[str, Any]], tuple[str, str]],
    batch_size: int,
    device: str | None,
) -> list[dict[str, Any]]:
    try:
        import torch
        import transformers
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("Stage classifier eval requires `pip install semsql-train[ml]`.") from e

    tokenizer = transformers.AutoTokenizer.from_pretrained(checkpoint)  # type: ignore[no-untyped-call]
    model = transformers.AutoModelForSequenceClassification.from_pretrained(checkpoint)
    dev = _resolve_device(torch, device)
    model.to(dev)
    model.eval()

    out: list[dict[str, Any]] = []
    with torch.no_grad():
        for batch in _batched(list(rows), batch_size):
            left: list[str] = []
            right: list[str] = []
            for row in batch:
                a, b = text_pair_fn(row)
                left.append(a)
                right.append(b)
            enc = tokenizer(
                left,
                right,
                max_length=128,
                truncation=True,
                padding=True,
                return_tensors="pt",
            )
            enc = {k: v.to(dev) for k, v in enc.items()}
            logits = model(**enc).logits.detach().cpu()
            for row, logit in zip(batch, logits, strict=True):
                score = _positive_score(logit.tolist())
                scored = dict(row)
                scored["score"] = score
                out.append(scored)
    return out


def _linker_metrics(
    rows: Sequence[dict[str, Any]],
    *,
    k: int,
    sample_examples: int,
) -> tuple[dict[str, float], list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["nl"])].append(row)

    recalls: list[float] = []
    ndcgs: list[float] = []
    runtime_recalls: list[float] = []
    entity_recalls: list[float] = []
    field_recalls: list[float] = []
    failures: list[dict[str, Any]] = []
    for nl, group in grouped.items():
        relevant = {
            str(row["candidate_target"])
            for row in group
            if float(row.get("relevance_label", 0.0)) >= 0.5
        }
        if not relevant:
            continue
        ranked = sorted(
            group,
            key=lambda row: (-float(row["score"]), str(row["candidate_target"])),
        )
        top = [str(row["candidate_target"]) for row in ranked[:k]]
        hit = len(relevant.intersection(top))
        recall = hit / len(relevant)
        recalls.append(recall)
        ndcgs.append(_ndcg(ranked, k=k))

        ranked_entities = [
            row for row in ranked if _linker_kind(row) == "entity"
        ]
        ranked_fields = [
            row for row in ranked if _linker_kind(row) == "field"
        ]
        top_runtime = {
            str(row["candidate_target"])
            for row in ranked_entities[:_RUNTIME_TOP_K_ENTITIES]
        }
        top_runtime.update(
            str(row["candidate_target"])
            for row in ranked_fields[:_RUNTIME_TOP_K_FIELDS]
        )
        runtime_recalls.append(len(relevant.intersection(top_runtime)) / len(relevant))

        relevant_entities = {
            str(row["candidate_target"])
            for row in group
            if float(row.get("relevance_label", 0.0)) >= 0.5
            and _linker_kind(row) == "entity"
        }
        if relevant_entities:
            entity_recalls.append(
                len(relevant_entities.intersection(top_runtime)) / len(relevant_entities)
            )

        relevant_fields = {
            str(row["candidate_target"])
            for row in group
            if float(row.get("relevance_label", 0.0)) >= 0.5
            and _linker_kind(row) == "field"
        }
        if relevant_fields:
            field_recalls.append(
                len(relevant_fields.intersection(top_runtime)) / len(relevant_fields)
            )

        if recall < 1.0 and len(failures) < sample_examples:
            failures.append(
                {
                    "nl": nl,
                    "recall": recall,
                    "missing": sorted(relevant.difference(top)),
                    "top": top,
                }
            )

    return (
        {
            "questions": float(len(recalls)),
            "recall_at_k": _mean(recalls),
            "ndcg_at_k": _mean(ndcgs),
            "k": float(k),
            f"runtime_recall_at_entities{_RUNTIME_TOP_K_ENTITIES}_fields{_RUNTIME_TOP_K_FIELDS}": _mean(
                runtime_recalls
            ),
            f"entity_recall_at_{_RUNTIME_TOP_K_ENTITIES}": _mean(entity_recalls),
            f"field_recall_at_{_RUNTIME_TOP_K_FIELDS}": _mean(field_recalls),
            "entity_questions": float(len(entity_recalls)),
            "field_questions": float(len(field_recalls)),
        },
        failures,
    )


def _slot_metrics(
    predictions: Sequence[dict[str, Any]],
    *,
    sample_examples: int,
) -> tuple[dict[str, float], list[dict[str, Any]]]:
    total = len(predictions)
    correct = 0
    by_kind: dict[str, list[int]] = defaultdict(list)
    failures: list[dict[str, Any]] = []
    for pred in predictions:
        ok = pred["pred_index"] == pred["correct_index"]
        correct += int(ok)
        kind = _slot_kind(str(pred["slot_name"]))
        by_kind[kind].append(int(ok))
        if not ok and len(failures) < sample_examples:
            failures.append(pred)

    metrics = {"top1_accuracy": _rate(correct, total)}
    for kind, vals in sorted(by_kind.items()):
        metrics[f"{kind}_top1_accuracy"] = _mean(vals)
    return metrics, failures


def _read_linker_groups(path: Path, limit_groups: int | None) -> list[dict[str, Any]]:
    if limit_groups is None:
        return list(_read_jsonl(path))
    groups: dict[str, list[dict[str, Any]]] = {}
    for rec in _read_jsonl(path):
        nl = str(rec.get("nl") or "")
        if nl not in groups:
            if len(groups) >= limit_groups:
                continue
            groups[nl] = []
        groups[nl].append(rec)
    return [row for group in groups.values() for row in group]


def _read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            row = json.loads(line)
            if isinstance(row, dict):
                yield row


def _format_linker_candidate(rec: dict[str, Any]) -> str:
    kind = str(rec.get("candidate_kind") or "")
    target = str(rec.get("candidate_target") or "")
    return f"{kind}: {target}" if kind else target


def _linker_kind(row: dict[str, Any]) -> str:
    kind = str(row.get("candidate_kind") or "").strip().lower()
    if kind in {"entity", "field"}:
        return kind
    target = str(row.get("candidate_target") or "")
    return "field" if "." in target else "entity"


def _positive_score(logits: Sequence[float]) -> float:
    if not logits:
        return float("-inf")
    if len(logits) == 1:
        return float(logits[0])
    return float(logits[1])


def _resolve_device(torch: Any, requested: str | None) -> Any:
    if requested:
        return torch.device(requested)
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


def _batched(items: Sequence[_T], size: int) -> Iterator[list[_T]]:
    size = max(1, size)
    for i in range(0, len(items), size):
        yield list(items[i : i + size])


def _take(items: Iterable[_T], limit: int | None) -> list[_T]:
    out: list[_T] = []
    for item in items:
        if limit is not None and len(out) >= limit:
            break
        out.append(item)
    return out


def _norm_sqlish(value: str) -> str:
    return " ".join(value.split()).strip()


def _slot_kind(slot_name: str) -> str:
    if slot_name.startswith("@entity"):
        return "entity"
    if slot_name.startswith("@field"):
        return "field"
    if slot_name.startswith("@val"):
        return "value"
    return "other"


def _ndcg(ranked: Sequence[dict[str, Any]], *, k: int) -> float:
    top = ranked[:k]
    ideal = sorted(
        ranked,
        key=lambda row: (-float(row.get("relevance_label", 0.0)), str(row["candidate_target"])),
    )[:k]
    dcg = sum(
        float(row.get("relevance_label", 0.0)) / math.log2(rank + 2)
        for rank, row in enumerate(top)
    )
    idcg = sum(
        float(row.get("relevance_label", 0.0)) / math.log2(rank + 2)
        for rank, row in enumerate(ideal)
    )
    return (dcg / idcg) if idcg > 0 else 0.0


def _mean(values: Iterable[float | int]) -> float:
    vals = [float(v) for v in values]
    return (sum(vals) / len(vals)) if vals else 0.0


def _rate(count: int, total: int) -> float:
    return (count / total) if total else 0.0
