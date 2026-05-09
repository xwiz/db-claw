"""Derive Stage 1 (linker) multi-entity pairs from a v3 skeleton corpus.

The shipping linker corpus (``data/linker_train.jsonl``) carries
single-item rankings: one entity or field per row, no cross-entity
disambiguation. Multi-table BIRD queries demand the linker rank multiple
candidates from the *same* schema slice — that's how Stage 2 sees both
entities surfaced. Without that, even a JOIN-trained Stage 2 can only
emit single-FROM because the linker hands it one entity.

This generator iterates v3 teacher-cache rows, harvests the
``ranked_schema`` of each row as a positive multi-item ranking, and
emits one Stage 1 record per entity/field/fk slot with cross-row
distractors so the cross-encoder learns to disambiguate within and
across rows.

Output shape matches ``trainers/linker.py`` expectations:
``{candidate_kind, candidate_target, db_id, gold_sql_hash, is_hard_negative,
nl, relevance_label, stage: 1}``.

Use:

    python -m semsql_train derive-linker-pairs \\
        --in data/skeleton_train_v3_ultimate.jsonl \\
        --out data/linker_train_v3.jsonl \\
        --max-rows 100000
"""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

__all__ = ["DeriveLinkerConfig", "derive_linker_pairs"]


@dataclass(frozen=True)
class DeriveLinkerConfig:
    negatives_per_positive: int = 3
    """Hard-negative candidates per positive. Cross-row distractors give
    the model exposure to off-topic schema items it should rank below
    the gold ranking."""

    seed: int = 0xCAFEF00D


def derive_linker_pairs(
    in_jsonl: Path,
    cfg: DeriveLinkerConfig,
    max_rows: int | None = None,
) -> Iterator[dict]:
    """Yield Stage 1 training records derived from a v3 skeleton corpus.

    Two-pass iteration: first builds per-kind pools so cross-row
    distractors are drawn from the same kind. Second pass emits records.
    """
    rng = random.Random(cfg.seed)
    entity_pool: list[str] = []
    field_pool: list[str] = []
    with in_jsonl.open("r", encoding="utf-8") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            for r in rec.get("ranked_schema") or []:
                kind = r.get("kind")
                target = r.get("target")
                if not isinstance(target, str):
                    continue
                if kind == "entity":
                    entity_pool.append(target)
                elif kind == "field":
                    field_pool.append(target)
            if len(entity_pool) > 50000 and len(field_pool) > 50000:
                break
    rng.shuffle(entity_pool)
    rng.shuffle(field_pool)

    seen = 0
    with in_jsonl.open("r", encoding="utf-8") as fh:
        for line in fh:
            if max_rows is not None and seen >= max_rows:
                break
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            nl = rec.get("nl") or ""
            db_id = rec.get("db_id") or ""
            ranked = rec.get("ranked_schema") or []
            if not nl or not ranked:
                continue
            gold_hash = hashlib.md5(
                (rec.get("natsql_skeleton") or "").encode("utf-8")
            ).hexdigest()[:12]

            row_entities = {
                r["target"] for r in ranked
                if r.get("kind") == "entity" and isinstance(r.get("target"), str)
            }
            row_fields = {
                r["target"] for r in ranked
                if r.get("kind") == "field" and isinstance(r.get("target"), str)
            }

            # Positive examples — every gold ranked-schema item.
            for r in ranked:
                kind = r.get("kind")
                target = r.get("target")
                if not isinstance(target, str) or kind not in ("entity", "field"):
                    continue
                yield {
                    "stage": 1,
                    "nl": nl,
                    "db_id": db_id,
                    "candidate_kind": kind,
                    "candidate_target": target,
                    "relevance_label": 1.0,
                    "is_hard_negative": False,
                    "gold_sql_hash": gold_hash,
                }
                seen += 1
                if max_rows is not None and seen >= max_rows:
                    return

            # Hard negatives — cross-row schema items that AREN'T in this
            # row's ranked_schema. Teaches the encoder to score off-topic
            # schema below the row's actual ranking.
            for _ in range(cfg.negatives_per_positive):
                if entity_pool:
                    pick = entity_pool[rng.randrange(len(entity_pool))]
                    if pick not in row_entities:
                        yield {
                            "stage": 1,
                            "nl": nl,
                            "db_id": db_id,
                            "candidate_kind": "entity",
                            "candidate_target": pick,
                            "relevance_label": 0.0,
                            "is_hard_negative": True,
                            "gold_sql_hash": gold_hash,
                        }
                        seen += 1
                        if max_rows is not None and seen >= max_rows:
                            return
                if field_pool:
                    pick = field_pool[rng.randrange(len(field_pool))]
                    if pick not in row_fields:
                        yield {
                            "stage": 1,
                            "nl": nl,
                            "db_id": db_id,
                            "candidate_kind": "field",
                            "candidate_target": pick,
                            "relevance_label": 0.0,
                            "is_hard_negative": True,
                            "gold_sql_hash": gold_hash,
                        }
                        seen += 1
                        if max_rows is not None and seen >= max_rows:
                            return
