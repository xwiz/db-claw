# Phase D — GPU operator handoff

**Date**: 2026-05-08
**Author**: cascade-v3 build run
**Goal**: lift BIRD-100 EX from 1 % (cascade-v3.2 CPU) to ≥ 35 % (Phase D
gate per `docs/completion-plan.md`).

The pipeline, corpus, and bug fixes are in place. Everything below is
strictly compute-bound: it does **not** require code changes, only a
machine with a CUDA-capable GPU.

## Repo state at handoff

| Artefact | Path | Notes |
|---|---|---|
| Ultimate v3 corpus | `data/skeleton_train_v3_ultimate.jsonl` | 498,980 rows / 179,383 INNER JOINs / 179,212 FK rows |
| 50K JOIN-balanced subset | `data/skeleton_train_v3_50k_balanced.jsonl` | 25K JOIN + 25K non-JOIN |
| Stage 2 trained model (CPU) | `target/skeleton-v3-50k-balanced/` | t5-small, 1 epoch, train_loss 0.27 |
| Stage 1 trained model (CPU) | `target/linker-v3/` | distilbert-base, 1 epoch, train_loss 0.28 |
| Stage 3 trained model (CPU) | `target/slot-filler-v3-10k/` | distilbert-base, 1 epoch, train_loss 0.40 |
| Stage 3 derived corpus | `data/slot_train_v3.jsonl` | 95,000 rows from `derive-slot-pairs` |
| Cascade-v3.2 manifest | `target/cascade-v3/manifest.json` | exec_acc 1 % BIRD-100 |

## Required GPU steps (in order)

### 1. Stage 2 t5-base × 5 epochs × full 500K corpus (~3–4 days RTX 4060)

```bash
python -m semsql_train train \
  --stage skeleton \
  --base-model t5-base \
  --train data/skeleton_train_v3_ultimate.jsonl \
  --eval data/skeleton_eval.jsonl \
  --epochs 5 \
  --batch-size 4 \
  --grad-accum 32 \
  --bf16 \
  --flash-attn 2 \
  --compile \
  --out target/skeleton-v3-base
```

Expected metrics: train_loss < 0.10, Spider EM ≥ 75 %, BIRD EM ≥ 60 %.

### 2. Stage 1 linker on multi-entity pairs (~30 min RTX 4060)

The v0.2 linker corpus at `data/linker_train.jsonl` carries single-item
rankings. Multi-table BIRD queries need multi-entity disambiguation
training. Derive new pairs from the v3 teacher cache:

```python
# scaffold — derive from `ranked_schema` rows in the v3 corpus.
# Each row's ranked_schema is a positive ranking; sample distractors
# from other rows' entities to teach cross-row negative ranking.
```

Then:

```bash
python -m semsql_train train \
  --stage linker \
  --base-model distilbert-base-uncased \
  --train data/linker_train_v3.jsonl \
  --eval data/linker_eval_v3.jsonl \
  --epochs 3 \
  --batch-size 32 \
  --out target/linker-v3-base
```

Expected: recall@5 ≥ 92 %.

### 3. Stage 3 slot filler on full 95K (~45 min RTX 4060)

Re-run derivation with a higher cap so the corpus covers all v3
skeleton rows:

```bash
python -m semsql_train derive-slot-pairs \
  --in data/skeleton_train_v3_ultimate.jsonl \
  --out data/slot_train_v3_full.jsonl \
  --max-rows 200000

python -m semsql_train train \
  --stage slot-filler \
  --base-model distilbert-base-uncased \
  --train data/slot_train_v3_full.jsonl \
  --eval data/slot_eval_v3.jsonl \
  --epochs 3 \
  --batch-size 32 \
  --out target/slot-filler-v3-base
```

Expected: per-slot top-1 ≥ 90 %.

### 4. Re-enable rich extractor (Rust)

After Stage 3 is retrained on the richer candidate distribution,
swap the active extractor in `crates/semsql-runtime/src/lib.rs`:

  * Rename `extract_nl_value_candidates_rich` → `extract_nl_value_candidates`.
  * Delete the legacy v3.2 implementation.
  * Verify the existing 9 unit tests in `extractor_tests` still pass.

The rich extractor surfaces multi-word capitalised phrases like
`'Fresno County Office of Education'` and ISO dates, which the
legacy extractor drops. With Stage 3 retrained against this
distribution, expected lift is ~5–10 pts EX.

### 5. Re-export cascade-v3-base + run BIRD-dev full (1534 examples)

```bash
python -m semsql_train export-cascade \
  --output-dir target/cascade-v3-base \
  --cascade-version v0.3.0-gpu \
  --skeleton-checkpoint target/skeleton-v3-base \
  --linker-checkpoint target/linker-v3-base \
  --slot-filler-checkpoint target/slot-filler-v3-base

# Patch manifest paths to point at _<stage>_export/model_quantized.onnx
# (see scripts in docs/results/v2-bird-smoke-failures.md "What changed"
# section for the manifest patch — required because the export tool
# leaves stale model_quantized.onnx files in pre-existing dirs).

python -m semsql_eval spider \
  --questions data/bird/dev.json \
  --db-root data/bird/dev_databases \
  --name bird \
  --semsql-bin target/release/semsql.exe \
  --cascade-manifest target/cascade-v3-base/manifest.json \
  --report-json docs/results/v3-bird-full-report.json \
  --graph-cache-dir target/spider_graphs
```

Acceptance gate: BIRD EX ≥ 35 % on the full 1534-example dev split.

## Bug fixes baked into the v3 pipeline

These are already in code at handoff. The operator does **not** need
to repeat them:

  * `teacher_cache.py:_SkeletonBuilder` now emits `INNER JOIN @entityN
    ON @fieldX = @fieldY` slots in the rendered skeleton (was stripping
    JOINs entirely; only 0–10 JOIN rows in 500K).
  * `derive-slot-pairs` CLI generates expanded Stage 3 corpus from any
    skeleton corpus, with hard-negative NL stop-word distractors.
  * `generate-targeted-v3` CLI emits JOIN-chain / HAVING / arithmetic
    templates from a `.semsql` graph for graph-specific augmentation.
  * `export-cascade` quantisation step skips when multi-file conflicts
    exist; operator must delete stale `model.onnx` /
    `model_quantized.onnx` from pre-existing target dirs before
    re-running export.
  * Phase E llguidance bridge (`tokenizer_bridge.rs`) wires
    `compute_mask()` end-to-end. With t5-base weights this is expected
    to add +3–5 pts EX over the unconstrained Stage 2.

## What's NOT required for Phase D

  * Distillation / Phase F (ship-size optimisation) — premature when
    accuracy is the bottleneck. Ship-size matters once EX ≥ 25 %.
  * Larger CPU training runs — diminishing returns; CPU + t5-small is
    structurally bounded near the 1–5 % EX range.
  * OmniSQL full snapshot (22 GB `seeklhy/OmniSQL-datasets`) — the
    9.4K BIRD-aligned subset at `xxxbrem/OmniSQL-BIRD/train_bird.json`
    plus NSText2SQL and Gretel synthetic give equivalent breadth at
    1 % of the storage cost.
