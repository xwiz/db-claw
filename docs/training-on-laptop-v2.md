# Training on laptop v2 — 2026 modern recipe

Supersedes `docs/training-on-laptop.md`. Same RTX 4060 Laptop target, but
swaps Spider 1.0 (saturated, annotation errors per CIDR 2026) for a 2026
training + eval stack:

| Phase | v1 (Spider 1.0)                  | v2 (this doc)                              |
|-------|----------------------------------|--------------------------------------------|
| Train | Spider train + BIRD dev (≈8k)    | **SQaLe** (≈518k triples, 91-table schemas)|
| Eval  | Spider dev (1034) + BIRD dev     | **BIRD dev** (1534) + BIRD-Critic (500)    |
| Gap   | Saturated benchmarks             | Honest enterprise gap (BIRD-Ent ≈39 % EX)  |

## Why v1 underperformed

Per-stage probe of the v1 cascade (`eval_per_stage.py`):

| Stage | v1 result    | Spec target | Gap   |
|-------|--------------|-------------|-------|
| 1 linker     | 80.99 % R@5   | ≥ 95 %      | -14   |
| 2 skeleton   | **16.18 % EM**| ≥ 85 %      | -69   |
| 3 slot-filler| 93.79 % top-1 | ≥ 90 %      | +4 ✅ |

Stage 2 is starved: only 6147 rows after the v0.2 NatSQL subset filter
(JOIN/HAVING/subquery rejection). t5-small overfits trivial single-table
queries and fails on the long tail. Fix is data, not architecture.

## Datasets — modern stack

### Training: SQaLe (`trl-lab/SQaLe-text-to-SQL-dataset`)
- 135,875 schemas / 517,676 validated triples
- Median 91 tables × 435 cols per schema (vs Spider 2.0: 7 × 89)
- 76 % of queries have JOINs; aggregations + subqueries + set-ops included
- Every query execution-validated via ReFoRCE self-refinement
- License: research-permissive; Apache-2.0-style

### Eval: BIRD dev (1534) + BIRD-Critic (500)
- BIRD ships sqlite DBs **via HF** (`nlile/BIRD-bench`), so `fetch-datasets
  --with-databases` unblocks exec-acc without Yale-Lily Google-Drive friction
- BIRD-Critic SQLite (March 2026) — 500 buggy queries; measures repair
  capability, maps onto `stage_skeleton.rs::repair_attempts`
- (Optional) Spider 2.0 / BIRD-Ent later for honest enterprise reporting

## NatSQL subset lift: v0.2 → v0.3

`teacher_cache.py` currently rejects:
- Multi-JOIN (>1 join) → reject
- LEFT/RIGHT/FULL/CROSS joins → reject
- HAVING → reject
- Subqueries / set ops / CTEs → reject

v0.3 changes (rationale: SQaLe ships FK graphs so multi-JOIN transcribe is safe):
1. **Allow up to 3 INNER JOINs** in a chain. Each entity registers in
   `ranked_schema`; alias map already in place.
2. **Allow HAVING** when the predicate references an aggregate that's
   already in the SELECT list. Render as `HAVING @aggN <op> @valN`.
3. Keep rejecting subqueries, CTEs, and set ops — those need real
   recursive grammar work, not a token swap.

Expected retention lift: 72 % → 90 %+ on Spider, even higher on SQaLe
(which is engineered for clean execution).

## The v2 recipe

### Phase A — data prep (one-time, ~30 min)
```powershell
# SQaLe download (~3-4 GB cached locally; HF caches under data/hf-cache/)
python -m semsql_train build-teacher-cache --sqale --out data/skeleton_train_sqale.jsonl

# BIRD eval data + DBs
python -m semsql_eval fetch-datasets --suite bird --with-databases --out data
```

### Phase B — re-train Stage 2 (~6-8 hours on RTX 4060)
```powershell
python -m semsql_train train --stage skeleton \
    --train data/skeleton_train_sqale.jsonl \
    --eval  data/skeleton_eval.jsonl \
    --out   target/checkpoints/skeleton-v2 \
    --base-model t5-small --epochs 2 --batch-size 16 --grad-accum 8 --bf16
```

Note: 2 epochs over 500k > 5 epochs over 6k. Active subset selection still
optional via `active-subset --target 25000` if VRAM/time-bound.

### Phase C — re-export + eval
```powershell
python -m semsql_train export-cascade \
    --output-dir target/cascade-v2 --cascade-version v0.6.0-rc1 \
    --linker-checkpoint      target/checkpoints/linker \
    --skeleton-checkpoint    target/checkpoints/skeleton-v2 \
    --slot-filler-checkpoint target/checkpoints/slot_filler

python -m semsql_eval spider --name bird \
    --questions data/bird/dev.json --db-root data/bird/dev_databases \
    --cascade-manifest target/cascade-v2/manifest.json
```

## Targets

| Stage | v1 actual | v2 target | Path                                     |
|-------|-----------|-----------|------------------------------------------|
| 1 linker     | 80.99 % R@5 | ≥ 92 %  | More epochs + harder negatives from SQaLe FKs |
| 2 skeleton   | 16.18 % EM  | ≥ 70 %  | SQaLe data lift + v0.3 subset             |
| E2E BIRD EX  | (untested)  | ≥ 50 %  | Enterprise-honest first run               |

## What remains out of scope

- Spider 1.0 dev exec-acc (Yale-Lily DBs are Google-Drive-gated; honest
  comparison no longer adds signal post-2026)
- Multi-turn (BIRD-Interact) — requires conversation state in the cascade
- Snowflake / BigQuery dialects (Spider 2.0-Snow) — different SQL surface
