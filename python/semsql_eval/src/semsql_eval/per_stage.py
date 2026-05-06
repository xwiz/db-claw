"""Per-stage evaluation harnesses.

The cascade is debuggable per stage *because* each stage has its own
metric. End-to-end exec-acc tells you whether the answer is right; per-
stage metrics tell you *which stage broke it*.

Stages and their canonical metrics:

  - **Stage 0a (pre-resolver)** — exact-NatSQL match on the subset of
    queries the resolver claims to handle. Target: ≥99% (deterministic).
  - **Stage 0b (intent library)** — pattern-precision on a curated
    idiom corpus. Target: ≥95%.
  - **Stage 1 (linker)** — recall@k (k = 5 by default) over schema
    items. The published RESDSQL target is ≥95% recall@5 on Spider.
  - **Stage 2 (skeleton)** — exact skeleton match (after slot
    placeholder normalisation). Target: ≥85%.
  - **Stage 3 (slot filler)** — per-slot top-1 accuracy. Target: ≥90%.

This module supplies the building blocks for each:

  - :func:`linker_recall_at_k` — group predictions by question,
    compute fraction of gold items that land in the top-k by score.
  - :func:`linker_ndcg_at_k` — discounted cumulative gain (graded
    relevance, useful when soft-label distillation is in play).
  - :func:`skeleton_exact_match` — skeleton match over a normalised
    placeholder vocabulary.
  - :func:`slot_top1_accuracy` — per-slot classification accuracy.

All functions take iterables of plain dicts so they're trivially
testable without spinning up torch.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

__all__ = [
    "RankedItem",
    "LinkerEvalResult",
    "linker_recall_at_k",
    "linker_ndcg_at_k",
    "skeleton_exact_match",
    "slot_top1_accuracy",
]


@dataclass(frozen=True)
class RankedItem:
    """One predicted ``(question, schema_item, score, gold_relevance)``."""

    question_id: str
    target: str
    score: float
    gold: float
    """``1.0`` for relevant, ``0.0`` for not. Soft labels in ``[0, 1]``
    are tolerated by ``ndcg`` but truncated to ``{0, 1}`` for recall."""


@dataclass(frozen=True)
class LinkerEvalResult:
    """Aggregate linker metrics across a corpus."""

    questions: int
    recall_at_k: float
    ndcg_at_k: float
    k: int


def linker_recall_at_k(items: Iterable[RankedItem], *, k: int = 5) -> LinkerEvalResult:
    """Compute recall@k grouped by ``question_id``.

    Per question:

      relevant = {target | gold ≥ 0.5}
      ranked   = top-k targets sorted by score (ties broken by target id)
      recall   = |relevant ∩ ranked| / |relevant|

    Macro-averaged across questions. Skips questions with no relevant
    items (unscored).
    """
    grouped: dict[str, list[RankedItem]] = defaultdict(list)
    for it in items:
        grouped[it.question_id].append(it)

    recalls: list[float] = []
    ndcgs: list[float] = []
    for qid, group in grouped.items():
        relevant = {g.target for g in group if g.gold >= 0.5}
        if not relevant:
            continue
        ranked = sorted(group, key=lambda it: (-it.score, it.target))[:k]
        hit = sum(1 for r in ranked if r.target in relevant)
        recalls.append(hit / len(relevant))
        ndcgs.append(_ndcg_at_k(group, k=k))

    return LinkerEvalResult(
        questions=len(recalls),
        recall_at_k=(sum(recalls) / len(recalls)) if recalls else 0.0,
        ndcg_at_k=(sum(ndcgs) / len(ndcgs)) if ndcgs else 0.0,
        k=k,
    )


def linker_ndcg_at_k(items: Iterable[RankedItem], *, k: int = 5) -> float:
    """Stand-alone nDCG@k aggregator. Same grouping as
    :func:`linker_recall_at_k` but returns just the nDCG."""
    grouped: dict[str, list[RankedItem]] = defaultdict(list)
    for it in items:
        grouped[it.question_id].append(it)
    scores = [_ndcg_at_k(group, k=k) for group in grouped.values()]
    return (sum(scores) / len(scores)) if scores else 0.0


def _ndcg_at_k(group: Sequence[RankedItem], *, k: int) -> float:
    """nDCG = DCG / IDCG, where DCG sums ``gold / log2(rank + 1)`` over
    the predicted top-k and IDCG is the same sum over the *ideal* top-k."""
    ranked = sorted(group, key=lambda it: (-it.score, it.target))[:k]
    ideal = sorted(group, key=lambda it: (-it.gold, it.target))[:k]
    dcg = sum(it.gold / math.log2(rank + 2) for rank, it in enumerate(ranked))
    idcg = sum(it.gold / math.log2(rank + 2) for rank, it in enumerate(ideal))
    return (dcg / idcg) if idcg > 0 else 0.0


# ---------------------------------------------------------------------------
# stage 2 / stage 3 metrics — small surface, no model needed
# ---------------------------------------------------------------------------


def skeleton_exact_match(
    pairs: Iterable[tuple[str, str]],
) -> float:
    """Fraction of ``(predicted, gold)`` skeleton pairs that match exactly
    after whitespace normalisation."""

    def norm(s: str) -> str:
        return " ".join(s.split())

    total = 0
    correct = 0
    for pred, gold in pairs:
        total += 1
        if norm(pred) == norm(gold):
            correct += 1
    return (correct / total) if total else 0.0


def slot_top1_accuracy(
    triples: Iterable[tuple[str, list[str], int]],
) -> float:
    """Stage 3 metric.

    Each triple is ``(slot_name, ranked_candidates_descending, correct_index)``;
    the slot is "correct" if the candidate at index ``correct_index`` is
    the model's top pick — i.e. the first candidate in the ranked list.
    """
    total = 0
    correct = 0
    for _slot, ranked, correct_idx in triples:
        total += 1
        if not ranked:
            continue
        # The ranked list is already sorted by score descending; the
        # top-1 is index 0. The slot is correct if the gold candidate is
        # at position 0.
        if 0 <= correct_idx < len(ranked) and ranked[0] == ranked[correct_idx]:
            correct += 1
    return (correct / total) if total else 0.0
