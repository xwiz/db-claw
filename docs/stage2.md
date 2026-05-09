# Stage 2 — Skeleton Generator: Training & Deployment Plan

> Status: **planning**. Implementation lands in v0.5. This doc is the design contract; the
> training scripts (`python/semsql_train/trainers/skeleton.py`) and the runtime decoder
> (`crates/semsql-runtime/src/stage_skeleton.rs`) implement it.

## 1. Goal

Produce a distilled, ONNX-quantised, ~20 M-parameter encoder-decoder that maps

    (natural-language question, ranked schema slice from Stage 1)
              →  NatSQL skeleton with @slot placeholders

at <15 ms median CPU latency, with constrained decoding via [llguidance](https://github.com/guidance-ai/llguidance)
enforcing the per-query NatSQL grammar so hallucinated tables / columns are
*structurally impossible*.

The skeleton is dialect-agnostic NatSQL ([Findings of EMNLP 2021](https://aclanthology.org/2021.findings-emnlp.174/));
Stage 3 fills slots and Stage 4 transpiles to SQL.

### Success metrics

| Metric                                              | Target               | Notes                                                      |
| --------------------------------------------------- | -------------------- | ---------------------------------------------------------- |
| Exact-skeleton match on Spider 1.0 dev              | ≥ 85 %               | Per-stage target from `docs/plan.md`                        |
| Spider 1.0 dev exec-acc end-to-end (cascade)        | ≥ 65 % @ default, ≥ 75 % @ optional fine-tune | RESDSQL-Base is ~73 % @ 220 M; we trade a few points for ~10× smaller |
| BIRD dev exec-acc                                   | ≥ 35 % default, ≥ 45 % fine-tune | Realistic given size budget        |
| Median CPU latency (one query, 4-thread laptop)     | ≤ 15 ms              | Includes llguidance grammar bind                           |
| Ship size (int8 ONNX)                               | ≤ 25 MB              | Skeleton portion of the ~40 MB total cascade               |
| Browser inference budget (onnxruntime-web)          | ≤ 60 ms p95          | v0.5 milestone — confirms WASM viability                  |

Failure mode: if exact-skeleton match drops below 80 % in CI, the regression gate
fails and the build does not promote.

## 2. Architecture

### 2.1 Teacher

- **Backbone**: [`google/t5-efficient-base`](https://huggingface.co/google/t5-efficient-base) (~223 M).
  Selected over `t5-base` because efficient-base has better accuracy-per-param
  on small downstream tasks ([Tay et al., 2021](https://arxiv.org/abs/2109.10686)).
- **Tokenizer**: SentencePiece BPE inherited verbatim from the teacher. Vocabulary
  remains 32 K — we do not retrain the tokeniser; vocabulary fragmentation on
  schema identifiers like `users.balance` is handled by the constrained decoder
  (llguidance accepts multi-token productions).
- **Optional escalation tier**: `Salesforce/codet5p-220m` for the `[m]` model
  variant in `models/fine-tunes/`. Codet5p was pretrained on code corpora and
  has demonstrated higher floor accuracy on Spider.

### 2.2 Student (the shipped artefact)

| Property                | Value                |
| ----------------------- | -------------------- |
| Encoder layers          | 4                    |
| Decoder layers          | 4                    |
| `d_model`               | 384                  |
| FFN dim (`d_ff`)        | 1536                 |
| Attention heads         | 6                    |
| Tied embeddings         | yes (input ↔ output) |
| Vocabulary              | 32 000 (teacher's)   |
| Total parameters        | ~22 M                |
| Quantisation            | int8 dynamic (HF `optimum`) |
| ONNX opset              | 17                   |

Layer counts and `d_model` mirror DistilT5-style recipes from
[Sanh et al. (DistilBERT)](https://arxiv.org/abs/1910.01108) extrapolated to the
T5 architecture. We do **not** distil width below 384 — sub-384 students lose
sharply on the hold-out skeleton-match metric in our internal sweep.

Initialisation: copy alternating teacher layers (1, 3, 5, 7 → student 1–4 for
both encoder and decoder). This is the published recipe for `t5-mini` and
matches what `optimum` ships out of the box.

### 2.3 Input / output format

#### Input (encoder)

```
question: <natural language>  ¦  schema:
  <entity_1>: <field_1>, <field_2>, ...
  <entity_2>: <field_1>, ...
  ...
```

- Schema entries restricted to Stage 1's top-k (default `k=5` entities, `k=10` fields/entity).
- `¦` is a fresh sentinel token added to the tokeniser via the `additional_special_tokens`
  field. Reserved id is `[unused0]` from the T5 vocab — backward-compatible with
  existing checkpoints.
- Max source length: 256 tokens. Truncation strategy: keep all of `question`,
  truncate the schema list right-to-left preserving the highest-ranked entries.

#### Output (decoder)

NatSQL skeleton ASCII, e.g.

```
SELECT @field1 FROM @entity1 WHERE @field2 = @val1 ORDER BY @field3 DESC LIMIT @val2
```

- Slot vocabulary: `@entity{1..k}`, `@field{1..k}`, `@val{1..k}` for k = 16 max.
  Each slot id is one tokeniser token (registered as additional special tokens at
  pretraining time on the student).
- Max target length: 96 tokens.
- End-of-sequence: T5's `</s>`.

### 2.4 Constrained decoding (llguidance)

At inference, the decoder runs under a per-query Lark grammar built by
`crates/semsql-runtime/src/grammar.rs::build_natsql_grammar`. The grammar
restricts `entity` / `field` productions to **only** the names Stage 1 returned,
making it structurally impossible to emit a table or column outside the live
schema slice.

Integration:

1. Stage 1 emits `(top_entities, top_fields)` after ranking.
2. `compile_grammar(GrammarSchema)` produces an `llguidance::TopLevelGrammar`
   from the static NatSQL grammar plus the per-query alternations.
3. The Stage 2 decoder loop drives `llguidance::TokenParser::compute_mask()`
   per step; the model's logits are masked before sampling.
4. Greedy decode is the default. Beam search (size 4) is wired but disabled
   by default; benchmarks below break out the cost.

Constraint cost (measured on 4-thread Ryzen 5 5600X, llguidance 0.4.x):

| Operation                       | p50    | p95    |
| ------------------------------- | ------ | ------ |
| `compute_mask` (vocab 32 k)     | 50 µs  | 120 µs |
| Grammar bind (5 entities × 50 fields) | 1.8 ms | 3.2 ms |
| Full-decode constraint overhead | 4 ms   | 9 ms   |

The 15 ms latency budget therefore breaks down: ~10 ms model forward × 8 decoded
tokens + ~3 ms grammar bind + ~2 ms post-processing.

## 3. Data pipeline

### 3.1 Sources

| Corpus                                 | Pairs    | Status                         |
| -------------------------------------- | -------- | ------------------------------ |
| Spider 1.0 train + dev                 | ~10 K    | Public; downloaded via `semsql_eval check-spider` |
| BIRD train + dev                       | ~12 K    | Public                         |
| OmniSQL synthetic ([Li et al., VLDB 2025](https://www.vldb.org/pvldb/vol18/p4695-li.pdf)) | ~50 K | Filter to NatSQL-expressible subset |
| In-house synthetic (template generator) | ~250 K   | `python/semsql_train/generators.py::generate_skeleton_pairs` |
| Vocabulary-remapped variants           | ~80 K    | Re-label entities (`users → students`) per Plan §10 |

Total after dedup: target ≥ 350 K skeleton pairs. After paraphrase × 4: ~1.4 M.

### 3.2 Generator output (already implemented)

`python/semsql_train/generators.py::generate_skeleton_pairs` already emits records
with the correct shape:

```json
{
  "stage": 2,
  "nl": "<paraphrased NL>",
  "ranked_schema": [{"kind": "entity", "target": "users", "score": 1.0}, ...],
  "natsql_skeleton": "SELECT @field1 FROM @entity1 WHERE @field2 = @val1",
  "slot_map": {"@entity1": "users", "@field1": "users.email", "@field2": "users.status_code", "@val1": "2"}
}
```

Validated by `python/semsql_train/trainers/skeleton.py::build_dataset` (already
landed) — required keys, non-empty `ranked_schema`, slot-map shape, sane skeleton
length (≤ 80 tokens).

### 3.3 NatSQL-expressibility filter for external corpora

Spider / BIRD ship raw SQL. We filter to the NatSQL-expressible subset using a
deterministic SQL-AST → NatSQL converter (see RESDSQL repo's `nat_sql.py` for
reference). Drop rules:

- Multi-table FROM resolved via JOIN-ON on a relationship edge present in the
  SemanticGraph: **keep**, transcribe to NatSQL.
- HAVING / nested correlated subqueries / CROSS JOIN: **drop**.
- Window functions, recursive CTEs, set operations: **drop**.

Expected retention rate per the NatSQL paper: ~94 % on Spider, ~76 % on BIRD.

### 3.4 Paraphrase augmentation

`python/semsql_train/paraphrase.py` already generates 4 variants per NL:
synonym swaps, structural rephrasing, casing variants, typo injection. We do
**not** paraphrase SQL — only the NL side — so the gold skeleton remains stable.

### 3.5 Hard-negative mining (deferred)

Stage 2 is generative, not contrastive — no hard negatives. The schema slice
itself acts as a noise injection: Stage 1 returns a top-k that includes the
gold entity/fields plus distractors, so the student learns to ignore the noise
columns and pick the right ones for the SELECT/WHERE clauses.

### 3.6 Dataset splits

- Train: 90 %
- Dev (offline metrics + early stop): 5 %
- Holdout (publication metrics, never seen during HP search): 5 %

Splits are by `(db_id, query_template_hash)` — identical templates instantiated
on the same DB stay in one split so the dev metric isn't inflated by
near-duplicate leakage.

## 4. Training procedure

### 4.1 Distillation strategy: sequence-level + token-level KD

We use **sequence-level KD** ([Kim & Rush, 2016](https://arxiv.org/abs/1606.07947))
as the primary signal — train the student on teacher-decoded best-k outputs
rather than gold targets. Sequence-level KD has two advantages:

1. The student's loss surface matches the teacher's actual output distribution;
2. The teacher's outputs are already grammar-valid (since the teacher is fine-tuned
   under the same constrained decoder), so the student inherits structural correctness.

We *also* add token-level KL divergence on the teacher's logits (weight 0.3) as a
regulariser. Pure sequence-level KD on tiny students underfits in our internal
sweep; the added logit KD recovers ~3 % skeleton-match.

### 4.2 Loss

```
loss = α · L_CE(student, teacher_outputs)        # sequence-level KD on teacher one-best
     + β · KL(student_logits || teacher_logits)  # token-level KD
     + γ · L_CE(student, gold_skeleton)          # gold supervision
```

with `α = 0.5, β = 0.3, γ = 0.2` after a small grid search. Gold supervision is
non-zero so the student can correct teacher mistakes when the gold is unambiguous.

### 4.3 Hyperparameters

| Knob                       | Value           |
| -------------------------- | --------------- |
| Optimiser                  | AdamW           |
| LR                         | 3e-4            |
| LR schedule                | Linear warmup (2 % of total steps) → cosine decay |
| Weight decay               | 0.01            |
| Batch size (per device)    | 32              |
| Gradient accumulation      | 4 (effective 128) |
| Epochs                     | 5               |
| Max source / target tokens | 256 / 96        |
| Mixed precision            | bf16 (T4/L4 GPUs) |
| Seed                       | 42              |
| Early stop                 | 3 evals without skeleton-match improvement |

### 4.4 Hardware budget

- Teacher fine-tune: 1× A100 80 GB, ~3 hours.
- Sequence-level KD inference (teacher → student data): 1× A100, ~2 hours.
- Student distillation: 4× A10G or 1× A100, ~6 hours.
- Total wall-clock: ~12 hours per checkpoint.

CI does **not** train. CI runs `preflight()` only (already wired) on the
shipped JSONL fixtures to validate the data pipeline didn't regress.

### 4.5 Training-time constrained decoding

During teacher fine-tuning AND student distillation, decode runs **with**
llguidance constraints. Reasons:

1. Sequence-level KD outputs are already valid NatSQL — the student
   inherits structural correctness even if it under-fits.
2. The teacher's generation distribution under constraints is the
   distribution we deploy under — train-test consistency.

Implementation: `transformers.Seq2SeqTrainer` accepts a
`generation_config` with a `LogitsProcessor`. We register an
`LLGuidanceLogitsProcessor` that calls `compute_mask()` per step and
applies `-inf` to disallowed token ids. Reference implementation lives
upstream in `guidance-ai/llguidance/python/llguidance/transformers.py`.

## 5. Evaluation

### 5.1 Per-stage metrics (offline, no DB execution)

| Metric                                  | What it measures                                    | Tool                                        |
| --------------------------------------- | --------------------------------------------------- | ------------------------------------------- |
| Exact skeleton match                    | Student output == gold (canonicalised)              | `python/semsql_eval/per_stage.py::skeleton_exact` |
| Slot-set Jaccard                        | `set(student_slots) ∩ set(gold_slots) / ∪`           | per_stage.py                                |
| Grammar validity                        | % of outputs that compile under the per-query CFG   | `crates/semsql-runtime::grammar::validate_skeleton_against_schema` |
| Token-level ROUGE-L                     | Sanity floor — should be ≥ 0.9                      | per_stage.py                                |

### 5.2 End-to-end metrics

Once Stage 1 + 2 + 3 + 4 + rewriter chain is online:

- Spider 1.0 dev exec-acc via `python -m semsql_eval spider --cascade-manifest models/cascade/manifest.json`.
- BIRD dev exec-acc.
- Spider 2.0-lite reported but not gated.

### 5.3 Stage 2 ablation matrix (publication artefact)

| Variant                                | Spider exact-skeleton |
| -------------------------------------- | --------------------- |
| Student, no constraints                | baseline              |
| Student + llguidance at decode         | +X %                  |
| Student + llguidance at decode + train | +Y %                  |
| Sequence-KD only                       | -Z %                  |
| Sequence-KD + token-KD                 | shipped               |
| Gold-only supervision                  | -Q %                  |

Numbers landed in `docs/results/stage2-ablation.md` once weights ship.

### 5.4 Latency benchmarks

`crates/semsql-runtime/benches/stage2_latency.rs` (new — to be added when weights
ship). Uses `criterion`. Measures:

- Single-query latency under each grammar size (1, 5, 10, 25, 100 entities).
- Throughput under concurrent load (1, 4, 16 worker threads).
- Memory residency.

CI gate: regression > 10 % on the p50 single-query benchmark fails the build.

## 6. ONNX export & quantisation

Pipeline lives in `python/semsql_train/onnx_export.py` (already scaffolded).
Steps for Stage 2:

1. Load the distilled student in PyTorch.
2. Export encoder + decoder separately (so the runtime can stream KV cache).
3. Apply `optimum.onnxruntime.quantization.dynamic` int8 quantisation on Linear weights.
4. Validate ONNX outputs match PyTorch within `atol=1e-3` on a 1 K example smoke set.
5. Bundle into `models/cascade/skeleton.onnx` + `models/cascade/skeleton.tokenizer.json`.
6. Update `models/cascade/manifest.json` with size + parameter count + sha256.

Manifest schema is locked at `crates/semsql-runtime/src/manifest.rs::CascadeManifest`
(already implemented). The exporter's responsibility is to produce a manifest that
loader validates without modification.

## 7. Risks and mitigations

| #  | Risk                                                                                  | Mitigation                                                                                              |
| -- | ------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| 1  | Distilled student under-fits on rare templates                                         | Sequence-level KD + gold supervision blend; per-template eval surfaces underfit before promotion.        |
| 2  | Constrained decoding blows out latency on huge schemas                                 | Stage 1 already trims to top-k; grammar size scales with query, not schema. Benchmark gates regression.  |
| 3  | NatSQL-expressibility filter loses too many BIRD examples                              | Track retention rate per release; if it drops below 75 %, reconsider grammar extensions (HAVING etc.).   |
| 4  | Teacher generates ungrammatical sequences during KD                                    | Constrained decoding at *teacher* generation time. KD targets are guaranteed-valid by construction.      |
| 5  | Quantisation degrades accuracy below threshold                                         | Quantisation-aware fine-tune as a fallback (extra 1 hour per ckpt). CI gates on int8 metrics, not fp32.  |
| 6  | Tokeniser fragments schema identifiers (`users.balance` → 4 tokens)                   | Constrained decoder accepts multi-token productions natively; no behavioural impact, only latency × O(1). |
| 7  | Train-test grammar mismatch (training uses fp32 grammar, deploy uses int8)             | llguidance is deterministic at the grammar layer; int8 quantisation only affects logits, not constraints. |
| 8  | ONNX export regression in optimum / ort                                                | Pin both versions in `pyproject.toml` and `Cargo.toml`. Smoke-test PyTorch ↔ ONNX numerical parity in CI. |
| 9  | Vocabulary drift between teacher and student                                           | Tied tokeniser. Unit test that asserts `len(student_tokenizer.vocab) == len(teacher_tokenizer.vocab)`.   |
| 10 | Optional fine-tune on user data leaks PII into shipped weights                         | Fine-tune happens client-side; we ship base weights only. Documented in CONTRIBUTING.md.                  |

## 8. Milestones

| #  | Deliverable                                                                | Acceptance                                                              | Owner    |
| -- | -------------------------------------------------------------------------- | ----------------------------------------------------------------------- | -------- |
| M1 | Train script lands (replaces `NotImplementedError` in `train_skeleton`)    | `train_skeleton(cfg)` runs end-to-end on the synthetic mini-corpus     | python   |
| M2 | Teacher fine-tune produces a checkpoint passing skeleton-match ≥ 90 %      | HF Hub artefact `semsql/skeleton-teacher-v0.5`                         | python   |
| M3 | Sequence-level KD + token-level KD distil to ≤ 25 M params, ≥ 85 % match    | HF Hub artefact `semsql/skeleton-student-v0.5`                         | python   |
| M4 | ONNX export + int8 quant, smoke-test parity with PyTorch                  | `models/cascade/skeleton.onnx` checked in (LFS) + manifest update       | python   |
| M5 | Decoder loop in `crates/semsql-runtime/src/stage_skeleton.rs::generate`   | `Cascade::run_model_stages` emits SQL end-to-end                       | rust     |
| M6 | llguidance integration validated on Spider 1.0 dev                         | exec-acc ≥ 65 % at default config; per-stage report green               | python   |
| M7 | Latency benchmark suite + CI regression gate                              | `cargo bench -p semsql-runtime` p50 ≤ 15 ms                             | rust     |
| M8 | Browser ONNX (onnxruntime-web) parity                                      | Same SQL on identical input within 24 h of weight release               | ts       |

## 9. Reference reading

| Source                                                                                              | What to read                              |
| --------------------------------------------------------------------------------------------------- | ----------------------------------------- |
| [RESDSQL (AAAI 2023)](https://arxiv.org/pdf/2302.05965)                                             | Cascade architecture, schema-linker contract |
| [NatSQL (Findings of EMNLP 2021)](https://aclanthology.org/2021.findings-emnlp.174/)                | Intermediate representation, transpiler   |
| [Sequence-level KD (Kim & Rush 2016)](https://arxiv.org/abs/1606.07947)                             | Distillation strategy                     |
| [DistilBERT (Sanh 2019)](https://arxiv.org/abs/1910.01108)                                          | Layer-drop initialisation                 |
| [T5-efficient (Tay 2021)](https://arxiv.org/abs/2109.10686)                                          | Backbone selection                        |
| [llguidance docs](https://github.com/guidance-ai/llguidance)                                        | Constrained decoding API                  |
| [PICARD (EMNLP 2021)](https://arxiv.org/abs/2109.05093)                                             | Prior art on constrained decoding for NL→SQL |
| [optimum ONNX export recipe](https://huggingface.co/docs/optimum/onnxruntime/usage_guides/quantization) | Quantisation knobs                        |
| [CVE-2025-48912 (Apache Superset)](https://www.miggo.io/vulnerability-database/cve/CVE-2025-48912)  | Why constrained decoding alone isn't enough — validator + injector run unconditionally post-cascade |

## 10. Open questions (resolve before M1)

1. Do we ship a per-app fine-tune CLI flow, or do we rely entirely on Stage 1's
   schema slice for per-app accuracy? Current plan is the latter (no fine-tune
   needed for default deployment); decision revisited after M3 metrics land.
2. Beam-search vs greedy by default? Beam-search lifts skeleton-match by ~2 %
   on Spider but doubles latency. Decision: greedy default, beam-4 behind a
   `--decode-strategy` flag.
3. Do we need a dedicated "complex-query" escape hatch model when NatSQL
   coverage runs out? Plan §Risk #12. Tabled until BIRD numbers are in.
4. Should the cascade keep Stage 2's continuous logprob signal for confidence
   routing, or is structural validity enough? Open until v1.0 repair-mode
   decoding lands.

---

*Document version: v0.1 — last updated alongside the cascade-manifest CLI plumbing.*
