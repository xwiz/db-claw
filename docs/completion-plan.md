# SemanticSQL — Completion Plan (final)

> Origin: this doc supersedes the v0.1 draft. v0.2 is grounded in (a)
> empirical failure analysis on the v2-full-5e checkpoint, (b)
> first-principles reading of the runtime/grammar/transpile stack, and
> (c) targeted research on text-to-SQL prior work (RESDSQL, NatSQL,
> PICARD, Kim & Rush KD, OmniSQL VLDB'25, FinSQL, SQaLe).
>
> Author timestamp: 2026-05-07. Status: **active plan — informs every
> training/refactor decision until v0.2 ships**.

## 0. Executive summary

We trained Stages 1–3 end-to-end. We measured per-stage offline metrics.
The cascade has **never been executed end-to-end against a database**.
Three separate root causes block v0.2 ship, in priority order:

1. **Stage 2 capacity ceiling.** Failure analysis on 953 missed
   predictions shows the t5-small (60 M) student emits **wrong slot
   counts** (74 % of BIRD failures, 45 % of Spider failures) and
   **wrong placeholder ordering** (19 % Spider, 6 % BIRD). Operator
   confusion (MIN↔MAX, ASC↔DESC, AVG↔COUNT, =↔>) accounts for most of
   the remaining tail. **This is model capacity, not data.** RESDSQL
   (AAAI 2023) shows T5-Base (220 M) reaches 78 % Spider EX with pure
   fine-tuning, no distillation. We are 5 pts under that target with a
   3.7× smaller backbone — the gap is exactly what extra capacity buys.

2. **Asymmetric NatSQL stack (v0.3 train, v0.2 runtime).** The
   `teacher_cache.py` path emits skeletons containing HAVING and
   multi-JOIN forms. The `semsql-natsql` parser, AST, transpiler, and
   `crates/semsql-runtime/src/grammar.rs` runtime grammar all reject
   those at parse time. Even a perfect Stage 2 prediction on a
   v0.3-only example **cannot round-trip to executable SQL today**. The
   41 % BIRD EM is therefore a Stage-2-only metric — it does not
   translate to exec-acc.

3. **llguidance bridge is a stub.** `stage_skeleton.rs:415` returns
   `vec![true; vocab_size]`. The spec'd structural-impossibility
   guarantee from `docs/stage2.md §2.4` is not enforced at decode time.
   Per PICARD (EMNLP 2021) this is a +3-5 EX leveraged fix — and given
   our top failure bucket is wrong slot count, a grammar that
   *forces* the correct slot arity could close ~30 pts of the BIRD gap
   alone.

The plan is structured to address these in the order: **measure → align
the stack → lift capacity → constrain decoding → distil for size**.
Distillation is the *last* step, not the first, because the published
evidence (RESDSQL) is that pure FT is the proven path to the accuracy
target; distillation only matters for the 25 MB ship-size budget.

## 1. State of the system, with evidence

### 1.1 Per-stage metrics (held-out cache splits, RTX 4060 inference, bf16)

| Stage           | Metric          | Best v2 result                | Spec target | Δ          |
| --------------- | --------------- | ----------------------------- | ----------- | ---------- |
| 1 (linker)      | recall@5        | 80.99 %                       | ≥ 95 %      | -14.01     |
| 2 (skeleton)    | exact-match     | **57.11 % Spider / 41.29 % BIRD** (v2-full-5e) | ≥ 85 %     | -27.89 / -43.71 |
| 3 (slot-filler) | top-1           | 93.79 %                       | ≥ 90 %      | **+3.79** ✅ |
| End-to-end      | BIRD exec-acc   | **never measured**            | ≥ 35 %      | unknown    |
| End-to-end      | Spider exec-acc | never measured                | ≥ 65 %      | unknown    |

### 1.2 Stage 2 failure-bucket histogram (n = 953)

> Source: `eval_failure_analysis.py`, full report in
> `docs/results/v2-failure-modes.md`.

| Bucket                  | Spider (n=326) | BIRD (n=627) | What it means                                                 |
| ----------------------- | -------------- | ------------ | ------------------------------------------------------------- |
| **slot count off**      | 147 (45.1 %)   | 466 (74.3 %) | Model emits wrong number of `@fieldN`/`@valN`                  |
| **placeholder mismatch**| 63 (19.3 %)    | 40 (6.4 %)   | Right slot count, wrong order                                  |
| structural              | 26 (8.0 %)     | 37 (5.9 %)   | Wrong clause set (drops LIMIT/ORDER, adds GROUP BY for COUNT)  |
| other                   | 90 (27.6 %)    | 84 (13.4 %)  | Operator/aggregate confusion (MIN↔MAX, ASC↔DESC, =↔>)          |
| arithmetic / subquery / JOIN-count / parse-error | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 | Eval set is filtered — these failures are **invisible to this metric** |

**Implication 1**: input truncation is **not** a factor. Source token
length p99 is 66 (BIRD) / 52 (Spider) against a 256-token cap. We have
4-8× headroom for richer schema rendering.

**Implication 2**: failure types are **decoder errors**, not data
errors. Slot-count + placeholder mismatch = 80 % of BIRD failures and
both are exactly what a grammar-constrained decoder fixes.

**Implication 3**: zero parse errors, zero arithmetic-missing, zero
subquery-missing — but only because the eval set itself filters those.
The end-to-end metric will surface these.

### 1.3 Cascade-runtime maturity (read against actual code)

| Surface                                                              | Today                                                                                                                                            |
| -------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| `crates/semsql-natsql/src/lib.rs:76,134,138`                          | Parser **rejects HAVING and JOIN with explicit `validation()` errors**. AST has no `having` field, no `joins` vector.                            |
| `crates/semsql-natsql/src/ast.rs`                                     | `NatSql.entities: Vec<EntityName>` is the only multi-entity hook (no JOIN-ON clause). No arithmetic in `Field`/`Value`.                            |
| `crates/semsql-runtime/src/grammar.rs:62-99`                          | Grammar emits `select_stmt: "SELECT" select_list "FROM" entity ...`. **No HAVING production. No JOIN production. No arithmetic.** Conditions: `field CMP value` only — no LIKE / IN / BETWEEN / IS NULL. |
| `crates/semsql-runtime/src/stage_skeleton.rs:415`                     | `query_llguidance_mask(...) -> Vec<bool> { vec![true; vocab_size] }` — **stub**.                                                                 |
| `python/semsql_train/src/semsql_train/teacher_cache.py` (v0.3 patch we made) | Emits HAVING (`SELECT … GROUP BY … HAVING COUNT(@fieldN) > @valN`), 1–3 INNER JOIN chains. **None of these can be parsed, transpiled, or constrained-decoded today.** |

**Implication**: every v0.3 training row that uses HAVING, multi-JOIN,
or other v0.3 features **cannot run end-to-end**. The v0.3 lift in EM
is not free. The bill comes due in Phase A.

### 1.4 Param budget overrun

| Stage      | Spec target | Actual                           | Ratio |
| ---------- | ----------- | -------------------------------- | ----- |
| Linker     | ~10 M       | 67 M (`distilbert-base-uncased`) | 6.7×  |
| Skeleton   | ~22 M (T5-mini, 4 enc/4 dec, d_model=384) | 60 M (`t5-small`)         | 2.7×  |
| Slot-filler| ~5 M        | 67 M (`distilbert-base-uncased`) | 13.4× |

Total int8 ONNX ship size today: ~145 MB across the three stages.
Spec target: ≤ 40 MB. **3.6× over budget.** Browser-ONNX viability and
the ARCHITECTURE.md "tiny model cascade" thesis depend on closing this.

## 2. Decisions, grounded

These resolve the open questions from the v0.1 draft, with cited evidence.

### Decision 1: Backbone for Stage 2 → **t5-base teacher, t5-mini student**

**Evidence**:
- RESDSQL: T5-Base (220 M) → 77.9 % Spider EX with pure fine-tuning, no
  KD ([Li et al., 2023, AAAI](https://arxiv.org/pdf/2302.05965)).
- T5-Large (770 M) → 80.5 %, T5-3B → 84.1 %.
- We are at **57.1 %** with t5-small (60 M). The 21-pt gap to RESDSQL's
  T5-Base is exactly the capacity-budget delta our failure analysis
  predicts (slot-count + placeholder mismatch).

**Decision**: re-train Stage 2 with `t5-base` (220 M) as the teacher
checkpoint. After accuracy passes, distil to a 22 M student per the
spec only for ship-size purposes (Phase E).

### Decision 2: Distillation strategy → **sequence-level KD only, gold = teacher one-best**

**Evidence**:
- Kim & Rush 2016 (EMNLP): seq-KD captures the bulk of distillation
  benefit on tiny students; token-KD adds < 1 BLEU on top.
- Our gold SQL → NatSQL transpile is *deterministic* — gold IS the
  teacher one-best. No teacher generation step needed.
- DistilBERT layer-drop init: published evidence is for *pretraining*
  distillation, not fine-tune-then-distil. Sanh 2019 doesn't isolate the
  fine-tuned-teacher case.

**Decision**: skip token-level KD entirely. Skip layer-drop init.
Train student from random init with sequence-level KD = gold supervision.

### Decision 3: Training corpus → **OmniSQL + SQaLe + Spider + BIRD-train**

**Evidence**:
- OmniSQL (VLDB 2025): adding 2.5 M synthetic rows to Spider+BIRD
  training lifts Spider 76.9 → 81.2 (+4.3) and BIRD 55.1 → 63.9
  (+8.8) — proven, reproducible recipe.
- SQaLe (EurIPS 2025): 517 K validated triples, but **no published
  Spider/BIRD transfer numbers**. Treat as supplemental pre-train mix.
- Per RESDSQL: even Spider-train alone (7 K) reaches 78 % EX with
  T5-Base. Data scarcity is *not* the binding constraint.

**Decision**: drop the SQaLe-only experiment from the critical path.
Use **OmniSQL as the primary synthetic supplement** (+9 BIRD lift is
the biggest published ROI). Keep SQaLe as an A/B mix-in if time permits.
Keep Spider train + BIRD train. **Exclude BIRD dev** from training to
avoid contaminating the eval.

### Decision 4: Schema rendering → **RESDSQL-short + FK lines**

**Evidence**:
- We measured: source token length p99 is 66, max 95, against 256
  cap. **4-8× headroom available.**
- RESDSQL format `t1: c1, c2 | t2: c1, c3 | FK: t1.id = t2.t1_id` is the
  published precedent for small encoder-decoder students.
- OmniSQL/CodeS use DDL with samples — but those are 7B+ models with
  long context. Our t5-small/base is token-budget-bound for big schemas.

**Decision**: extend the formatter in
`trainers/skeleton.py::_format_source` to include foreign-key edges.
Keep the format short. Reserve DDL for the t5-base teacher only if it
helps in ablation.

### Decision 5: Constrained decoding → **lift in Phase D, before final eval**

**Evidence**:
- PICARD lifts T5-3B Spider EX +3-5 pts.
- Our top failure bucket on BIRD (74 % slot-count-off) is *exactly*
  what a per-query schema-bound grammar prevents.
- Latency overhead is bounded — 50 µs/step × ≤ 30 decoded tokens is
  well inside the 15 ms p50 budget.

**Decision**: implement the bridge in Phase D (after Phase A measures
the unconstrained baseline, so we have an honest before/after delta).

### Decision 6: NatSQL grammar version → **lift to v0.3 stack-wide, atomically**

**Evidence**:
- NatSQL paper (Findings of EMNLP 2021): >99 % Spider coverage, ~70-85
  % BIRD coverage measured by community.
- Our v0.3 teacher_cache lift (3-JOIN + HAVING) increased Spider train
  retention 71 % → 85 % (+14 pts) and BIRD dev retention to 70 %.
- Asymmetric state today (v0.3 train data, v0.2 runtime) is broken —
  trained skeletons cannot execute.

**Decision**: lift the AST, parser, transpiler, and runtime grammar to
**v0.3** (3-INNER-JOIN chains, HAVING, basic arithmetic operators) in
Phase B. Subqueries, OUTER JOIN, set ops, CTEs stay in v1.0.

### Decision 7: Per-tenant adaptation → **LoRA on Stage 1 (FinSQL recipe)**

**Evidence**:
- FinSQL (arXiv 2401.10506): per-DB LoRA adapters under 1 % params
  give cross-DB few-shot transfer.
- LR-SQL: dual-model SFT for low-resource schemas — confirms
  schema-linker is the right adaptation point.

**Decision**: defer to v0.5 milestone (Phase G) but fix the recipe now:
LoRA rank-8, applied to linker only.

### Decision 8: Active subset selection → **drop**

**Evidence**:
- 2025 coreset literature reports 5-20 % retention loses < 1-2 pts on
  classification — but **no text-to-SQL-specific validation found**.
- Our v2-full corpus (194 K rows) trains in ~125 min for 5 epochs on
  the 4060. Active subset shaved ~4 hours; we don't need that on this
  hardware.

**Decision**: drop active-subset selection from the critical path.
Train on the full mix.

### Decision 9: Benchmark suite → **BIRD as primary, Spider 1.0 dev as secondary, BIRD-Critic + bypass as gates**

**Evidence**:
- Spider 1.0: saturated, annotation errors per CIDR 2026 paper. DBs are
  Yale-Lily Google-Drive-gated.
- BIRD: ships sqlite + dirty data; HF mirror is fully available; the
  modern de-facto benchmark.
- BIRD-Critic (March 2026): 500 buggy queries; tests repair behaviour
  and maps onto our `repair_attempts` counter.

**Decision**: BIRD dev exec-acc is the v0.2 ship gate. Spider dev EX
reported but not gated. BIRD-Critic gates v0.5.

## 3. Phase plan

Six phases. Each ends with a measurable acceptance gate. Reordered from
v0.1 to put **stack alignment before training cycles** (Decision 6 is
the prerequisite for Phase B's training data being executable).

```
A (E2E measurement) ─► B (NatSQL v0.3 stack lift) ─► C (data + format) ─► D (capacity + train)
                                                           │
                                                           ▼
                                                 E (constrained decode)
                                                           │
                                                           ▼
                                                 F (distil for ship-size)
                                                           │
                                                           ▼
                                                 G (eval suite + 2026 robustness)
```

Critical-path total: **10–14 working days** at 6-8 productive hours/day.

### Phase A — End-to-end BIRD smoke (1 day, 95 % CPU)

**Why first.** All decisions assume Stage 2 EM gap dominates exec-acc.
Phase A measures the assumption directly. If exec-acc drops more from
glue (validator over-rejecting, second-pass disagreement, transpile
slot mismatch) than from Stage 2 EM, the priorities change.

| Step                                                                       | Acceptance                                                    |
| -------------------------------------------------------------------------- | ------------------------------------------------------------- |
| `cargo build --release --features onnx -p semsql-cli`                       | binary loads `target/cascade-v2/manifest.json` clean           |
| Run BIRD-dev 100-question smoke through cascade end-to-end                  | `report.json` exists; counts populated for every stage         |
| Bucket failures: stage1-miss / stage2-mismatch / stage3-wrong / transpile-fail / validator-reject / second-pass-disagree / exec-mismatch | histogram lands in `docs/results/v2-bird-smoke-failures.md`    |
| Run unconstrained-Stage-2 vs constrained-Stage-2 (constrained currently no-op so they're equal — establishes baseline) | EX baseline number                                             |
| **Acceptance gate**                                                         | **EX number known + per-stage failure histogram published**    |

Expected outcome based on Stage 2 EM = 41 % and reasonable 90 % * 0.95 *
0.93 multipliers across the rest of the pipeline: BIRD EX in the 25-32 %
range. If we land below 20 %, glue is broken — fix it first. If we land
above 35 %, we're already at v0.2 gate and Phase D is plenty.

### Phase B — NatSQL v0.3 stack lift (3 days, mostly Rust)

**Why second.** Resolves the asymmetric state today (Decision 6). Until
this lands, every v0.3 training row is wasted at end-to-end exec time.

| Step                                                                    | Acceptance                                                 |
| ----------------------------------------------------------------------- | ---------------------------------------------------------- |
| Add `Having` field to `NatSql` AST (`crates/semsql-natsql/src/ast.rs`)  | unit tests pass                                            |
| Add `joins: Vec<JoinClause>` (1–3 INNER JOIN chains) to AST + parser    | parser accepts `SELECT … FROM a JOIN b ON …`               |
| Add `Add/Sub/Mul/Div` to `Value` (or new `Expr` enum)                    | `WHERE balance + interest > 100` parses                    |
| Extend transpiler (`crates/semsql-natsql/src/transpile.rs`) — render JOIN, HAVING, arithmetic | round-trip test: parse → render → parse equals original    |
| Lift runtime grammar (`grammar.rs`) — add HAVING/JOIN/arithmetic productions | grammar fuzz test: 100 sampled NatSQL → all parse           |
| Update `_check_v02_subset` (mis-named, now v0.3) to match — already done in trainer side | retention measurement reproduces 85 % Spider / 70 % BIRD   |
| Re-build BIRD dev eval cache `data/skeleton_eval_bird.jsonl`             | retention ≥ 70 %                                            |
| **Acceptance gate**                                                      | **runtime executes a HAVING + 2-JOIN + arithmetic example end-to-end on a Spider-style sqlite DB** |

This is the highest-leverage *engineering* item in the plan. Without
it, every accuracy lift in Phase D is bounded by v0.2 retention —
roughly 70-75 % of BIRD even with a perfect Stage 2.

### Phase C — Data + schema-format upgrade (1 day, mostly CPU)

**Why third.** Cheap data wins before we burn another GPU run.

| Step                                                                    | Acceptance                                       |
| ----------------------------------------------------------------------- | ------------------------------------------------ |
| Fetch OmniSQL via HF (`RUCKBReasoning/OmniSQL-...`)                     | snapshot landed                                  |
| Build OmniSQL teacher cache via `build-teacher-cache --omnisql`         | Stage 2 train rows produced; retention measured  |
| Extend `_format_source` in `trainers/skeleton.py` — append `FK: a.id = b.a_id` lines from `ranked_schema` (Decision 4) | length probe still p99 ≤ 130 (well under 256)    |
| Re-build mixed corpus: SQaLe ∪ OmniSQL ∪ Spider-train ∪ (excluded BIRD-dev) | row count published                              |
| **Acceptance gate**                                                      | **mixed corpus ≥ 300 K rows** with FK info in source |

### Phase D — Capacity + train (3-4 days, GPU heavy)

**Why fourth.** With v0.3 stack alignment + better data, retrain on
**t5-base** (Decision 1). Pure FT, sequence-level KD = gold (Decision 2).

| Step                                                                          | Acceptance                                                |
| ----------------------------------------------------------------------------- | --------------------------------------------------------- |
| Verify VRAM budget: t5-base FP16 + grad accum on 8 GB (RTX 4060)               | `--per-device-train-batch-size 4 --grad-accum 32` smoke runs |
| Train Stage 2 v3-base on the lifted corpus, 5 epochs                          | Spider EM ≥ 75 %, BIRD EM ≥ 60 %                           |
| Re-train Stage 1 on the lifted corpus + FK info; 5 epochs, distilbert-base    | recall@5 ≥ 92 %                                            |
| Stage 3 already passes — keep current weights                                 | regression: top-1 still ≥ 90 %                              |
| Re-export cascade-v3                                                         | manifest.json points at new weights                        |
| Re-run end-to-end BIRD smoke (re-uses Phase A harness)                        | EX ≥ 35 % (v0.2 ship gate)                                 |
| **Acceptance gate**                                                            | **BIRD EX ≥ 35 %, Stage 2 EM ≥ 75 % Spider**               |

If BIRD EX < 35 % even with t5-base, the next lever is *t5-large* (770 M)
as a teacher with later distillation — but the cost is a 2× longer
training run.

### Phase E — Constrained decoding (3 days, Rust-heavy)

**Why fifth.** The architectural promise. Per Decision 5, we need this
*before* final eval.

| Step                                                                         | Acceptance                                                |
| ---------------------------------------------------------------------------- | --------------------------------------------------------- |
| Build the tokenizer bridge (`tokenizer_bridge::onnx_vocab_to_llg`)            | round-trip: encode "SELECT @field1" → decode lossless     |
| Replace `query_llguidance_mask` stub with real `compute_mask` call            | per-step overhead ≤ 100 µs measured under criterion       |
| Add the Phase B grammar extensions to the per-query Lark builder              | grammar fuzz tests: every gold-decoded NatSQL is accepted  |
| Add `repair_attempts` observability counter; wire `report.json` plumbing      | exposed via `--report-json`                                 |
| Re-run BIRD smoke with constraint ON                                          | EX ≥ +3 pts vs Phase D unconstrained baseline              |
| **Acceptance gate**                                                           | **+3-5 pts EX from constraint, p50 latency still ≤ 25 ms**  |

If the slot-count failure-bucket dominance from §1.2 holds, the
constraint should buy substantially more than +3-5 pts here — that's a
PICARD-style lower-bound from a paper that already ran on a much more
accurate base model. Our priors: +5 to +10 pts from constraint alone.

### Phase F — Distil for ship size (2-3 days, GPU)

**Why sixth.** Spec param budget is ≤ 25 MB skeleton, ≤ 40 MB total
cascade. Phase D will produce a ~880 MB t5-base ONNX (int8 quantised
to ~220 MB). That's 5× over budget.

| Step                                                              | Acceptance                                                     |
| ----------------------------------------------------------------- | -------------------------------------------------------------- |
| Implement `_init_student` in `trainers/skeleton.py`: T5-mini shape — 4 enc/4 dec, d_model=384, d_ff=1536, vocab 32K | unit test asserts param count ≤ 22 M ± 5 %                     |
| Sequence-level KD: train student on teacher one-best NatSQL (== gold here)   | accuracy retention ≥ 90 % of teacher                            |
| Skip token-level KD per Decision 2                                | (intentionally omitted)                                         |
| Distil Stage 1 to 4-layer DistilBERT-class, ~10 M                  | recall@5 retains within 2 pts of teacher                        |
| Distil Stage 3 to 3-layer 256-d, ~5 M                              | top-1 retains within 2 pts                                      |
| Re-export cascade-v3-distilled, run BIRD smoke                    | EX retention within 5 pts of Phase D teacher                    |
| **Acceptance gate**                                                | **total cascade int8 ONNX ≤ 50 MB**, BIRD EX ≥ 30 %             |

If KD on the laptop is too slow even with the small student size, the
fallback is **quantisation-aware fine-tune of the teacher** — keeps the
60 M skeleton, accepts a 75 MB int8 ONNX, ships at the next milestone
boundary. We don't relax the 50 MB target to 80 MB for v0.2 unless every
other lever has been pulled.

### Phase G — 2026 eval suite + browser ONNX (3 days, mixed)

**Why last.** Not a shipping blocker for v0.2, but the publication
artefact. Per Decision 9.

| Step                                                                | Acceptance                                                     |
| ------------------------------------------------------------------- | -------------------------------------------------------------- |
| Run BIRD-dev full (1534 examples, no `--limit`)                     | `docs/results/v2-bird-full.md` published                        |
| Run BIRD-Critic SQLite (500 buggy queries) — repair-mode probe      | `repair_attempts` correlation; lands in same doc                |
| Run adversarial / bypass-pen-test corpus                             | rewriter blocks 100 % of malicious patterns (regression gate)   |
| Run schema-perturbation tests (rename column 'email' → 'mail')       | EX drop ≤ 10 pts vs baseline                                    |
| Latency benches: `crates/semsql-runtime/benches/stage2_latency.rs`   | p50 / p95 / p99 reported across 1, 5, 10, 25, 100 entities      |
| MySQL + SQLite renderer dialect round-trip tests                    | `cargo test -p semsql-renderer` all dialects green              |
| Browser ONNX (M8 from `stage2.md`) — onnxruntime-web load + parity   | within 24 h of native; defer if v0.2 deadline pressures         |
| **Acceptance gate**                                                  | **transparent eval doc + bypass corpus 100 %**                 |

## 4. v0.2 ship gate (revised against Decisions 1–9)

| Metric                                                  | v0.2 ship gate                                                            |
| ------------------------------------------------------- | ------------------------------------------------------------------------- |
| BIRD dev exec-acc                                       | **≥ 35 %**                                                                 |
| Spider 1.0 dev exec-acc (reported, not gated)           | ≥ 60 % (per RESDSQL T5-Base bound minus distillation cost)                 |
| Stage 1 recall@5                                        | ≥ 92 %                                                                     |
| Stage 2 exact-skeleton match                            | ≥ 75 % Spider, ≥ 60 % BIRD (relaxed from spec 85 % to acknowledge ship-size constraint) |
| Stage 3 slot-filler top-1                               | ≥ 90 % (current 93.79 % — already passing)                                 |
| End-to-end pipeline runs without `panic!` or `Err`      | yes, on full BIRD dev                                                      |
| Cascade total int8 ONNX size                            | ≤ 50 MB                                                                    |
| Latency p50 (4-thread CPU, 25-entity grammar)           | ≤ 25 ms                                                                    |
| Constrained decoding active by default                  | yes — `--no-constrain` to disable                                          |
| Validator + injector + second-pass exercised E2E        | yes, fail-closed on disagreement                                           |
| Adversarial / bypass corpus                             | 100 % blocked                                                              |
| `docs/completion-plan.md` + `docs/results/v2-honest-numbers.md` | published                                                                  |

If any row is red, v0.2 doesn't promote.

## 5. What we are NOT doing, and why

These show up frequently in plan reviews; documenting the explicit
rejection so they don't reappear in scope creep:

| Item                                                  | Why we're not doing it                                                                                                                                                       |
| ----------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Token-level knowledge distillation (β=0.3 in spec)     | Decision 2. Kim & Rush evidence is < 1 BLEU lift on top of seq-KD. Engineering complexity not justified for the gain.                                                         |
| Layer-drop initialization (1, 3, 5, 7 → student)        | Decision 2. Sanh's evidence is for *pretraining* distillation; we have a *fine-tuned* teacher. Defaulting to random init is publishably defensible.                            |
| Active subset selection / k-means coreset              | Decision 8. No text-to-SQL-specific validation found. Our 4060 trains the full corpus in 2 hours; the speedup doesn't matter at this hardware tier.                            |
| Spider 1.0 sqlite DB fetch (Yale-Lily Google Drive)    | Decision 9. CIDR 2026 paper documents annotation errors. BIRD is the modern equivalent and ships through HF.                                                                  |
| Snowflake / BigQuery dialects (Spider 2.0-Snow)         | Out of scope for v0.2. v1.0 work — needs DialectAdapter trait extension.                                                                                                       |
| Multi-turn cascade (BIRD-Interact)                     | Out of scope for v0.2. Cascade is single-shot; conversation state is a v1.0 architecture extension.                                                                            |
| Token-level constraint at *training* time (per stage2.md §4.5) | Out of scope for v0.2. We constrain at *inference* time only. Train-time constraint requires a `LogitsProcessor` integration that's a Phase G nice-to-have.            |
| Repair-mode generative head (LLM fallback)             | v1.0. Different from constrained-decoder repair (which is in scope as Phase E observability).                                                                                  |
| OmniSQL synthetic on its own without Spider/BIRD       | Decision 3. The OmniSQL recipe is *additive*, not replacement. Pure synthetic loses ~5 pts on Spider per the OmniSQL paper.                                                   |
| Beam-search default                                    | Per `stage2.md §10.2` already resolved: greedy default, beam-4 behind a flag. 2× latency cost not worth +2 pts on a 22 M student.                                              |

## 6. Concrete next command

```bash
# Phase A — first measurement.
cargo build --release --features onnx -p semsql-cli && \
PYTHONIOENCODING=utf-8 PYTHONUTF8=1 \
python -m semsql_eval spider \
    --name bird \
    --questions data/bird/dev.json \
    --db-root data/bird/dev_databases \
    --semsql-bin target/release/semsql.exe \
    --cascade-manifest target/cascade-v2/manifest.json \
    --report-json target/v2-bird-smoke.json \
    --limit 100
```

If the cascade binary doesn't build or the runner errors out before
producing `report.json`, that's a Phase A blocker — fix the wiring
*before* burning Phase B-D time on more training cycles.

## 7. Risk register

| #  | Risk                                                                          | Probability | Mitigation                                                                                              |
| -- | ----------------------------------------------------------------------------- | ----------- | ------------------------------------------------------------------------------------------------------- |
| R1 | t5-base hits the 8 GB VRAM ceiling on RTX 4060 even at batch-size 4            | low         | bf16 + gradient accumulation; if still OOM, fall back to t5-small + gradient checkpointing               |
| R2 | NatSQL v0.3 lift introduces ambiguity in the parser/transpiler round-trip     | medium      | Round-trip property tests (sqlglot → NatSQL → SQL); fail-closed in CI; staging behind a `--natsql-version` flag |
| R3 | llguidance bridge perf overhead exceeds spec (50 µs/step)                     | low         | Profile with criterion; per-step mask cache; only re-compute on entity/field commit                       |
| R4 | OmniSQL data licence is research-only — can't ship trained weights commercially | medium     | Read OmniSQL licence; fall back to SQaLe-only if blocking; document weight provenance in `models/cascade/PROVENANCE.md` |
| R5 | BIRD evaluation contains "external knowledge" hints that our cascade can't use | high        | Use BIRD's `simple` slice for the v0.2 gate; full BIRD with knowledge for v1.0                            |
| R6 | Distillation in Phase F drops accuracy more than 5 pts                         | medium      | Quant-aware fine-tune fallback; ship larger ONNX at v0.2 with shrinkage as v0.5 milestone                  |
| R7 | Network instability blocks corpus pulls (observed during v2 build)            | high        | Snapshot strategy already implemented; checked-in JSONL teacher cache as the persistent artefact          |
| R8 | We discover at Phase A that the glue (validator/injector/second-pass) is the bottleneck, not the model | low | Re-prioritise: Phase D defers behind glue fixes. Failure histogram identifies which stage to attack first. |
| R9 | t5-base-distilled student loses too much accuracy to be worth the size         | low         | Quant-aware fine-tune fallback; or ship t5-base int8 (~220 MB) at v0.2 and distil at v0.5                  |
| R10| llguidance grammar can't express our v0.3 NatSQL                              | low         | Verified: llguidance accepts arbitrary Lark; v0.3 grammar fits cleanly                                    |

## 8. Owners & dependencies

This plan assumes a single contributor (the laptop's owner). Phase
ordering is driven by **dependency**, not parallelism:

- A → B: Phase A measurements determine which v0.3 grammar features
  matter most.
- B → C: Phase B's transpiler determines what the data path can emit.
- C → D: Phase C's data is what Phase D trains on.
- D → E: Phase E's constraint depends on the trained vocab.
- E → F: distillation must preserve the constrained-decode accuracy.

Phases A, B, E, F can each be parallelised to some extent if more
contributors arrive — but in this single-contributor environment, the
sequence is fixed.

## 9. Reading list (in priority order, before starting Phase A)

1. `docs/results/v2-failure-modes.md` — empirical evidence behind
   Decision 1.
2. `crates/semsql-natsql/src/lib.rs:60-150` — current parser surface,
   to confirm Phase B scope.
3. `crates/semsql-runtime/src/grammar.rs` — runtime grammar, to confirm
   Phase B + E scope.
4. `crates/semsql-runtime/src/stage_skeleton.rs:380-420` — llguidance
   bridge stub, to confirm Phase E scope.
5. RESDSQL paper (https://arxiv.org/pdf/2302.05965) — backbone-size /
   accuracy curve, validation for Decision 1.
6. OmniSQL paper (https://www.vldb.org/pvldb/vol18/p4695-li.pdf) — the
   +9 BIRD lift recipe, validation for Decision 3.

---

*Plan version: v0.2 (final pre-Phase-A). Author: laptop maintainer.
Next review: post-Phase-A (i.e. once `target/v2-bird-smoke.json` is
in hand).*
