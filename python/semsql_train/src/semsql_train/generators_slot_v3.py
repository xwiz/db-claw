"""Derive Stage 3 (slot filler) training pairs from v3 teacher-cache rows.

The shipping Stage 3 corpus (``data/slot_train.jsonl``) carries only ~278
rows. With that little data the cross-encoder picks NL stop-words like
``'highest'``, ``'students'``, ``'opened'`` for the ``@val`` slot — the
dominant Phase A failure mode (~100% of BIRD-100 wrong).

This generator takes v3 teacher-cache rows (each carries
``natsql_skeleton`` + ``slot_map`` + ``ranked_schema``) and emits one
Stage 3 record per slot. For every slot we synthesize a candidate set
that includes:

  * The gold value (``slot_map[slot]``).
  * Plausible distractors from the NL question — quoted strings,
    capitalised tokens, numerics — *plus* stop-word noise so the model
    learns to score capitalised content above grammatical fillers.
  * Schema distractors for ``@entityN`` / ``@fieldN`` slots (other
    entities/fields from ``ranked_schema``).

The output is JSONL in the shape :func:`semsql_train.trainers.slot_filler`
expects: ``{stage: 3, nl, skeleton, slot_name, candidates, correct_index}``.

Use:

    python -m semsql_train derive-slot-pairs \\
        --in data/skeleton_train_v3_ultimate.jsonl \\
        --out data/slot_train_v3.jsonl \\
        --max-rows 50000
"""

from __future__ import annotations

import json
import random
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

__all__ = ["DeriveSlotConfig", "derive_slot_pairs"]


_QUOTED_RX = re.compile(r"'([^']{1,40})'|\"([^\"]{1,40})\"")
_NUM_RX = re.compile(r"\b\d+(?:\.\d+)?\b")
# Multi-word capitalised phrases with up to 3 small connectors
# ("of", "the", "and", "for", "in", "at"). Mirrors the Rust
# `extract_nl_value_candidates_rich` logic so derived training data
# matches the runtime candidate distribution exactly.
_CAP_RX = re.compile(
    r"\b[A-Z][a-zA-Z\-]+(?:\s+(?:of|the|and|for|in|at|[A-Z][a-zA-Z\-]+)){0,4}\s*[A-Z][a-zA-Z\-]+\b"
    r"|\b[A-Z][a-zA-Z\-]+\b"
)
_HYPHENATED_NUM_RX = re.compile(r"\b[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+\b")
_TOKEN_RX = re.compile(r"\b[A-Za-z]+\b")

# A small fixed set of NL stop-words and common verbs that bird/spider
# slot-fillers consistently mis-pick. We hand them in as hard negatives so
# the cross-encoder learns to score them below capitalized / numeric
# alternatives.
_HARD_NEG_NL_TOKENS = (
    "show", "list", "give", "find", "the", "of", "in", "with",
    "by", "for", "all", "and", "or", "is", "are", "have", "has",
    "highest", "lowest", "many", "most", "least", "top", "bottom",
    "average", "total", "name", "names", "number", "students",
    "opened", "closed", "active", "inactive",
)


@dataclass(frozen=True)
class DeriveSlotConfig:
    candidates_per_slot: int = 6
    """Total candidate set size per row (gold + distractors)."""

    seed: int = 0xCAFEF00D


def derive_slot_pairs(
    in_jsonl: Path,
    cfg: DeriveSlotConfig,
    max_rows: int | None = None,
) -> Iterator[dict]:
    """Yield Stage 3 training records derived from a v3 skeleton corpus.

    Two-pass over the input — first pass collects per-slot-kind value
    pools so single-row candidate sets can be padded with cross-row
    distractors. This ensures every emitted record has ≥ 2 candidates
    even when the source row's ``ranked_schema`` only surfaced one
    entity (common in NSText2SQL).
    """
    rng = random.Random(cfg.seed)
    entity_pool: list[str] = []
    field_pool: list[str] = []
    val_pool: list[str] = []
    with in_jsonl.open("r", encoding="utf-8") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            for slot, gold in (rec.get("slot_map") or {}).items():
                if slot.startswith("@entity"):
                    entity_pool.append(gold)
                elif slot.startswith("@field"):
                    field_pool.append(gold)
                else:
                    val_pool.append(gold)
            if len(entity_pool) > 50000 and len(field_pool) > 50000:
                break
    rng.shuffle(entity_pool)
    rng.shuffle(field_pool)
    rng.shuffle(val_pool)

    seen = 0
    with in_jsonl.open("r", encoding="utf-8") as fh:
        for line in fh:
            if max_rows is not None and seen >= max_rows:
                break
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            slot_map = rec.get("slot_map") or {}
            if not slot_map:
                continue
            nl = rec.get("nl") or ""
            skeleton = rec.get("natsql_skeleton") or ""
            ranked = rec.get("ranked_schema") or []
            for slot, gold in slot_map.items():
                cands = _make_candidates(slot, gold, nl, ranked, cfg, rng)
                if slot.startswith("@entity"):
                    pool = entity_pool
                elif slot.startswith("@field"):
                    pool = field_pool
                else:
                    pool = val_pool
                _pad_with_pool(cands, gold, pool, cfg.candidates_per_slot, rng)
                if len(cands) < 2:
                    continue
                if gold not in cands:
                    continue
                rng.shuffle(cands)
                yield {
                    "stage": 3,
                    "nl": nl,
                    "skeleton": skeleton,
                    "slot_name": slot,
                    "candidates": cands,
                    "correct_index": cands.index(gold),
                }
                seen += 1
                if max_rows is not None and seen >= max_rows:
                    return


def _pad_with_pool(
    cands: list[str],
    gold: str,
    pool: list[str],
    target: int,
    rng: random.Random,
) -> None:
    """Top up ``cands`` from ``pool`` until length ≥ target."""
    if not pool:
        return
    attempts = 0
    while len(cands) < target and attempts < target * 4:
        pick = pool[rng.randrange(len(pool))]
        if pick != gold and pick not in cands:
            cands.append(pick)
        attempts += 1


def _make_candidates(
    slot: str,
    gold: str,
    nl: str,
    ranked: list[dict],
    cfg: DeriveSlotConfig,
    rng: random.Random,
) -> list[str]:
    cands: list[str] = [gold]
    if slot.startswith("@entity"):
        for r in ranked:
            if r.get("kind") == "entity":
                t = r.get("target")
                if isinstance(t, str) and t and t != gold and t not in cands:
                    cands.append(t)
    elif slot.startswith("@field"):
        for r in ranked:
            if r.get("kind") == "field":
                t = r.get("target")
                if isinstance(t, str) and t and t != gold and t not in cands:
                    cands.append(t)
    else:
        # @valN — pull plausible NL tokens + hard-neg stop-words.
        # Order mirrors the Rust runtime's rich extractor so the
        # cross-encoder training distribution matches inference.
        for m in _QUOTED_RX.finditer(nl):
            tok = (m.group(1) or m.group(2) or "").strip()
            quoted = f"'{tok}'"
            if quoted and quoted != gold and quoted not in cands:
                cands.append(quoted)
        for m in _NUM_RX.finditer(nl):
            tok = m.group(0)
            if tok and tok != gold and tok not in cands:
                cands.append(tok)
        for m in _CAP_RX.finditer(nl):
            tok = m.group(0).strip()
            quoted = f"'{tok}'"
            if quoted and quoted != gold and quoted not in cands:
                cands.append(quoted)
        # Hyphenated codes / dates (K-12, 5-17, 2000-01-01).
        for m in _HYPHENATED_NUM_RX.finditer(nl):
            tok = m.group(0).strip()
            quoted = f"'{tok}'"
            if quoted and quoted != gold and quoted not in cands:
                cands.append(quoted)
        # Hard negatives — bare NL tokens that the heuristic candidate
        # extractor surfaces. Teaching the model to rank these BELOW
        # quoted/numeric tokens is the whole point of the expanded
        # corpus.
        nl_lower = nl.lower()
        for tok in _HARD_NEG_NL_TOKENS:
            if tok in nl_lower and tok != gold and tok not in cands:
                cands.append(tok)
        for m in _TOKEN_RX.finditer(nl):
            tok = m.group(0)
            if (
                tok.lower() in _HARD_NEG_NL_TOKENS
                and tok != gold
                and tok not in cands
            ):
                cands.append(tok)

    # Trim + shuffle so gold isn't always at index 0.
    if len(cands) > cfg.candidates_per_slot:
        kept = [gold] + [c for c in cands[1:] if c != gold]
        cands = [gold] + kept[1 : cfg.candidates_per_slot]
    rng.shuffle(cands)
    return cands
