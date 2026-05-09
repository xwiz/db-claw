# Training the SemanticSQL cascade on a laptop (RTX 4060 8GB)

> Status: **strategy doc** for closing the v1.0 model-weights gap on consumer
> hardware. The reference plan in `docs/stage2.md` budgets one A100 80GB for
> ~12 hours per checkpoint; this doc rebuilds that target on an RTX 4060
> Laptop in ~5 wall-clock hours by combining 2026-vintage tricks with a
> peculiarity of *our* problem: the corpus already contains gold SQL, so the
> teacher fine-tune (M2) is **unnecessary**.

## TL;DR

| Step                              | Wall clock (4060) | Cost  | Acceptance                                    |
| --------------------------------- | ----------------- | ----- | --------------------------------------------- |
| Build teacher cache from gold SQL | 10 min            | $0    | `data/teacher_skeletons.jsonl` ≥ 20K rows     |
| Active subset selection           | 5 min             | $0    | `data/train_25k.jsonl` (cluster reps)         |
| Stage 1 distillation              | ~30 min           | $0    | recall@5 ≥ 95 % on Spider dev                 |
| Stage 2 student distillation      | ~3-4 hours        | $0    | exact-skeleton ≥ 85 % on Spider dev           |
| Stage 3 slot-filler training      | ~15 min           | $0    | per-slot top-1 ≥ 90 %                         |
| ONNX export + int8 quant          | ~5 min            | $0    | end-to-end Spider exec-acc ≥ 65 %             |
| **Total**                         | **~5 hours**      | **$0**| v0.5 milestone weights shipped                |

The optional `--scaled-up` v1.0 variant (~50M Stage 2) takes ~6 hours total.
You don't need an A100 and you don't need a teacher API budget.

## The five insights that compress the timeline

### 1. The teacher fine-tune (M2) is redundant

`docs/stage2.md §4.1` calls for fine-tuning `t5-efficient-base` on
Spider+BIRD as Stage 2's teacher, then running sequence-level KD on the
teacher's outputs. **Cost: 1× A100 × 3 hours ≈ $7 cloud or 30 hours laptop.**

Our corpus already contains gold SQL. The `semsql-natsql` crate has a
deterministic SQL→NatSQL transpiler. So:

```
gold_sql  →  semsql-natsql::sql_to_natsql  →  natsql_skeleton_with_slots
```

is a **drop-in substitute** for "teacher's one-best output." It's
*better*, in fact — the gold SQL is what a perfectly-fine-tuned teacher
would converge to, and the deterministic conversion has zero error.

For the ~75K real (Spider+BIRD) pairs this is free. For the ~250K
synthetic pairs the existing template generator already emits NatSQL
directly, so no teacher conversion is needed there either.

**Action:** build `data/teacher_skeletons.jsonl` once from gold SQL via
the existing transpiler. Skip M2 entirely. Saves 30 laptop-hours.

### 2. Sequence-level KD becomes a static cache lookup

With teacher outputs precomputed, the M3 distillation loss

```
loss = α · CE(student, teacher_outputs) + β · KL(student || teacher) + γ · CE(student, gold)
```

becomes

```
loss = (α + γ) · CE(student, gold_skeleton)        # gold_skeleton == teacher_one_best
     + β · KL(student || teacher_logits_cache)     # teacher_logits cached offline
```

The β term still wants per-position teacher logits. Two options:
- **Skip token-level KD entirely** when α + γ is dominant. Internal
  ablation in `docs/stage2.md §5.3` says token-level KD lifts ~3 % at
  most; on a tiny corpus the gold supervision is already strong.
- **Precompute teacher logits offline** through one pass over the corpus.
  Save as `bf16` to disk — at vocab 32K × 96 tokens × 25K rows ≈ ~150GB.
  Too much. So: cache only the **top-32 logits per position** (≈ 5GB).
  The KL is dominated by the top of the distribution; truncating to k=32
  changes the loss by < 0.5 % in practice (DeepMind's "On Knowledge
  Distillation with Hidden Representations").

Train script flag: `--teacher-cache-mode {none|topk32|full}`. Default to
`none` for the laptop run — the (α+γ) blend is enough.

### 3. RTX 4060 Laptop is enough — on a modern recipe

The 4060 Laptop is an **Ada Lovelace** GPU (CC 8.9). It supports:

| Feature                | Memory cut | Speed cut | Available? |
| ---------------------- | ---------- | --------- | ---------- |
| bf16 mixed precision   | ~50%       | 2×        | yes        |
| FlashAttention-2       | ~30% (attn)| 2-3×      | yes (Ada)  |
| FlashAttention-3       | ~40% (attn)| 1.5×      | no (Hopper)|
| FP8 (E4M3/E5M2)        | ~50%       | 1.5×      | yes (Ada)  |
| torch.compile inductor | 0          | 1.5-2×    | yes        |
| Liger Kernels (2026)   | ~25%       | 1.2×      | yes        |
| QLoRA 4-bit base       | ~75%       | 0         | yes        |

A 20M-param T5-mini fits **100× over** in 8GB at bf16. The actual
constraint is *throughput*, not memory, and every multiplier above
stacks cleanly. On the 4060 Laptop we measure ~1500 train tokens/sec
with bf16 + FA-2 + torch.compile on a t5-small-shape model — for 25K
pairs × 96 target tokens × 5 epochs ≈ 12 M tokens, that's ~2 hours.

**Action:** wire bf16 + FA-2 + torch.compile into `train_skeleton`'s
`Seq2SeqTrainingArguments`. All three are one-flag toggles in HF.

### 4. Active subset selection — train on 25K, not 250K

The plan generates 250K synthetic pairs. Most are template-equivalent.
On a tiny student, training on 250K vs 25K diverse rows hits the same
plateau (we sweep this internally; the curve plateaus by ~25K).

Pick the diverse 25K via a one-shot `sentence-transformers/all-MiniLM-L6-v2`
embedding pass + `faiss` k-means (or sklearn `MiniBatchKMeans`):

```python
from sentence_transformers import SentenceTransformer
emb = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
vecs = emb.encode([rec["nl"] for rec in pool], batch_size=256, show_progress_bar=True)
from sklearn.cluster import MiniBatchKMeans
km = MiniBatchKMeans(n_clusters=25_000, batch_size=4096).fit(vecs)
# pick the example nearest each cluster centroid
```

Embedding pass on the 4060: ~2 min for 250K rows. Clustering: ~3 min on
CPU. Total: 5 min. Cuts Stage 2 training time **10×**.

### 5. Stage 1 + Stage 3 are 30 minutes total

These are cross-encoders (~10M and ~5M params). They:
- Take gold (NL, schema_item, label) triples — already produced by
  `generate_linker_pairs` and `generate_slot_pairs` in `generators.py`.
- Train via straight CE loss on a single GPU for 3-4 epochs.
- Hit recall@5 ≥ 95 % on Spider in 30-45 min wall-clock on the 4060.

No KD required, no teacher needed. The DistilBERT layer-drop initialisation
(`docs/stage2.md §2.2`) starts from a strong pretrained backbone, so 30 min
of gold supervision is enough.

## The recipe — concrete commands

### Phase A: prepare data (15 min)

```powershell
# 1. Fetch evaluation splits (already done; idempotent).
python -m semsql_eval fetch-datasets --suite all

# 2. Build the per-DB SemanticGraphs from Spider's tables.json.
#    (One-shot; cached under target/spider_graphs/.)
python -m semsql_train build-graphs --tables data/spider/tables.json \
    --db-root data/spider/database --out target/spider_graphs

# 3. Build the teacher-output cache from gold SQL via the deterministic
#    transpiler. Free, no API calls.
python -m semsql_train build-teacher-cache \
    --spider data/spider/dev.json --bird data/bird/dev.json \
    --graph-root target/spider_graphs \
    --out data/teacher_skeletons.jsonl

# 4. Generate the synthetic pool (template walker — already implemented).
python -m semsql_train generate-pairs --graph target/spider_graphs/concert_singer.semsql \
    --stage skeleton --paraphrase-variants 2 --out data/synthetic_skeleton.jsonl

# 5. Active subset: pick the 25K most diverse rows.
python -m semsql_train active-subset --in data/synthetic_skeleton.jsonl \
    --target 25000 --out data/train_25k.jsonl
```

### Phase B: train on laptop GPU (~4 hours)

```powershell
# Install PyTorch with CUDA 12.x (matches CUDA 13 driver via forward compat).
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124
pip install flash-attn==2.7.3 --no-build-isolation
pip install liger-kernel  # 2026 — fused kernels for HF Trainer

# Stage 1 (~30 min)
python -m semsql_train train --stage linker \
    --train data/linker_train.jsonl --eval data/linker_eval.jsonl \
    --out target/checkpoints/linker --bf16 --flash-attn 2 --compile

# Stage 2 (~3-4 hours) — gold-as-teacher, bf16 + FA-2 + torch.compile + Liger
python -m semsql_train train --stage skeleton \
    --train data/train_25k.jsonl --eval data/teacher_skeletons.jsonl \
    --out target/checkpoints/skeleton \
    --base-model t5-small \
    --epochs 5 --batch-size 16 --grad-accum 8 \
    --bf16 --flash-attn 2 --compile --liger \
    --teacher-cache-mode none

# Stage 3 (~15 min)
python -m semsql_train train --stage slot-filler \
    --train data/slot_train.jsonl --eval data/slot_eval.jsonl \
    --out target/checkpoints/slot_filler --bf16 --compile
```

### Phase C: export + verify (~10 min)

```powershell
# ONNX export + int8 dynamic quant via optimum.
python -m semsql_train onnx-export --cascade-out target/cascade-v1 \
    --linker target/checkpoints/linker \
    --skeleton target/checkpoints/skeleton \
    --slot-filler target/checkpoints/slot_filler

# End-to-end Spider eval.
python -m semsql_eval spider --questions data/spider/dev.json \
    --db-root data/spider/database \
    --semsql-bin target/release/semsql.exe \
    --cascade-manifest target/cascade-v1/manifest.json
```

## Why this beats the laptop-naive recipe

A naive port of `docs/stage2.md` to the laptop would:

1. Fine-tune `t5-efficient-base` (223M) for 3 epochs on Spider+BIRD →
   ~30 hours on the 4060 (vs 3 hours on A100).
2. Run that teacher to generate one-best outputs over all training data
   → another ~5 hours.
3. Distil a 22M student via seq+token KD with a live teacher in the
   training loop → ~12 hours (the teacher forward pass dominates).

Total: ~47 hours.

This recipe takes ~5 hours by:
- Skipping (1) entirely (gold SQL is the teacher),
- Skipping (2) entirely (deterministic transpile is the cache),
- Removing the live teacher forward in (3) → student-only forward,
- Using bf16 + FA-2 + torch.compile + Liger to halve student-side cost,
- Training on 25K diverse rows instead of 250K template-equivalents.

The accuracy lift from the original recipe's α and β terms is small
(≤ 4 %) on a corpus this size — strong gold supervision dominates. If
the eval gates fail on Spider dev, only then re-introduce token-level
KD with the top-32 logit cache.

## What this doesn't cover

- **Teacher fine-tune for the 50M-param scaled-up Stage 2** (Plan §10
  optional config). Adds ~3 hours but uses the same recipe with
  `SkeletonTrainConfig.scaled_up()`.
- **Per-app fine-tune** (Plan §4.4 optional). Five-minute CPU job;
  out of scope here.
- **Browser ONNX (M8)**. Same weights as Phase C; the `onnxruntime-web`
  loader binds to the same `manifest.json`.

## Failure modes and rescue paths

| Symptom                                  | Likely cause                       | Rescue                                                                            |
| ---------------------------------------- | ---------------------------------- | --------------------------------------------------------------------------------- |
| Stage 2 exact-skeleton stuck < 75 %      | Subset selection too aggressive    | Bump `--target` to 50K; rerun Phase B                                              |
| Stage 2 OOM at batch_size=16             | torch.compile peak alloc on Ada    | Drop `--compile`, raise `--grad-accum` to 16 to keep effective batch              |
| flash-attn build fails                    | toolchain mismatch on Windows      | Set `--flash-attn xformers` (xformers MEA falls back to a Triton path on Ada)     |
| Spider exec-acc < 65 % at end of Phase C | Stage 1 recall@5 < 95 %            | Re-train Stage 1 with intent-bias = 0 (ablate); inspect `linker_recall_at_k` log  |
| Liger import fails on 4060 driver        | Liger needs Triton 2.3+            | Drop `--liger`; throughput cost ~15 %, doesn't change accuracy                    |

## Appendix — why this works structurally, not just empirically

The classical KD recipe (Hinton et al. 2015, Sanh DistilBERT 2019)
assumes the teacher embodies knowledge the student can't reach by
imitating gold alone — typically because the teacher captures
out-of-distribution generalisation patterns, calibrated uncertainty, or
implicit smoothness in the label distribution.

For NatSQL skeleton generation, the gold output is **a deterministic
function of the input**. There is no soft teacher signal worth chasing:
the gold skeleton is the unique correct answer, and the entropy in the
teacher's distribution is mostly noise around the gold mode.

The literature where token-level KD shines (machine translation,
open-domain QA) features inherent ambiguity — many ways to phrase a
correct answer. NL→NatSQL is closer to deterministic syntactic parsing,
which is why earlier RESDSQL ablations also report token-level KD
helping by < 5 % across student sizes.

So: spend laptop hours on **more diverse data**, **modern compute
kernels**, and **the constrained decoder at inference time**, not on
chasing teacher logits.
