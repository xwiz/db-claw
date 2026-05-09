# Phase A — BIRD 100Q Smoke Test Failure Histogram

**Date**: 2026-05-07  
**Model**: cascade-v2 (`target/cascade-v2/manifest.json`)  
**Binary**: `target/release/semsql.exe` (onnx feature, v0.1.0-dev)  
**Dataset**: BIRD dev.json, first 100 questions, DBs: `california_schools` (89q), `financial` (11q)

## Summary metrics

| Metric | Value |
|--------|-------|
| Total | 100 |
| Correct (EX) | 0 |
| Wrong | 100 |
| Bailed | 0 |
| Errored | 0 |
| bail\_rate | 0.00 % |
| exec\_acc | 0.00 % |
| Stage pinned | stage\_3 = 100 |

## Failure root-cause histogram

All 100 questions exit with code 0 (valid SQL, no bail). All 100 produce
wrong execution results. Failures are **overlapping** — most questions hit
several root causes simultaneously.

| Root cause | Count | Notes |
|---|---|---|
| **Gold requires JOIN** | 71 | NatSQL v0.2 has no JOIN; Phase B adds `INNER JOIN` chains |
| **Gold requires arithmetic** | 20 | `CAST(a AS REAL) / b`, division ratios, `IIF` — Phase B adds basic arithmetic |
| **Wrong WHERE value** | ~100 | Slot filler picks wrong NL token for `@val` — e.g. `'County'` instead of `'Alameda'` |
| **Wrong SELECT field** | ~100 | Skeleton generator picks wrong columns for projection |
| **Gold uses subquery** | 7 | Nested `SELECT` in `WHERE`; not planned until v1.0 |
| **Gold requires HAVING** | 1 | Phase B adds HAVING |
| **Gold uses `DISTINCT`** | ~10 | Transpiler doesn't emit `DISTINCT`; Phase B+ |

## Per-failure-mode breakdown

### JOIN missing (71 / 100)

Every BIRD gold query with a JOIN is guaranteed wrong because NatSQL v0.2
disallows `JOIN`. The skeleton generator cannot emit a FROM with multiple
tables. Example:

```sql
-- gold
SELECT COUNT(DISTINCT T2.School)
FROM satscores AS T1
INNER JOIN schools AS T2 ON T1.cds = T2.CDSCode
WHERE T1.AvgScrMath > 400

-- pred
SELECT COUNT(schools.virtual) FROM schools
WHERE schools.virtual = 'are' AND schools.virtual > 'are'
```

**Fix**: Phase B — add `JoinClause` to AST, parser, transpiler, grammar.

### Wrong @val slot filling (~100 / 100)

The slot filler cross-encoder scores `(nl | skeleton, candidate)` pairs
where candidates come from NL token extraction. It consistently picks
wrong tokens — syntactic words like `'are'`, `'that'`, `'the'` rather
than named entities like `'Alameda'` or numeric literals like `400`.

Root causes:
1. Model trained on Spider; BIRD entity names are out-of-distribution.
2. NL token pool includes stop words that score above named entities.
3. No candidate ranking heuristics (e.g., prefer capitalized nouns for
   string comparisons, prefer numbers for numeric columns).

**Fix**: Phase D — retrain Stage 3 on OmniSQL + BIRD corpora.  
**Short-term workaround**: candidate pre-filter: prefer quoted capitals for
text slots, prefer digits-only for numeric slots.

### Wrong @field slot selection (~100 / 100)

Stage 1 ranks schema items by cross-encoder score. The top-2 entities and
top-4 fields are handed to Stage 2. Stage 2 then picks `@entity1`,
`@field1` etc. from those slots. The skeleton generator systematically
picks the wrong field for SELECT (e.g., `schools.virtual` when the gold
projects `schools.School`) and wrong entity for FROM.

Root causes:
1. Stage 2 trained on Spider — distribution shift on BIRD.
2. top\_k = 2 entities, 4 fields limits exposure of correct items.
3. No llguidance constraint (Phase E stub): decoder repeats tokens freely.

**Fix**: Phase B+C — retrain Stage 2 on OmniSQL; Phase E — llguidance
constraint cuts hallucination space to schema items only.

### Arithmetic / CAST (20 / 100)

Queries like "eligible free rate" require:
```sql
CAST(`Free Meal Count (K-12)` AS REAL) / `Enrollment (K-12)`
```
The NatSQL v0.2 AST has no `Arithmetic` expression node; the transpiler
can only emit `SELECT field` or `SELECT AGG(field)`.

**Fix**: Phase B — add `Arithmetic` to AST + parser + transpiler.

## Glue vs model breakdown

The Phase A hypothesis was: if glue (validator, second-pass, transpile) is
the bottleneck rather than model quality, fix glue before burning Phase B
training cycles.

| Glue layer | Failures attributed | Notes |
|---|---|---|
| Transpile (Stage 4 parse error) | 0 | All 100 produce parseable SQL after fallbacks |
| Schema validator reject | 0 | validate\_skeleton\_against\_schema falls through to bare parse |
| Second-pass disagree | 0 | Not wired (Python rewriter not called by eval harness) |
| Execution error (SQLite) | 0 | 100 % executable after entity-mismatch fix |
| Execution wrong | 100 | Semantic errors — model quality, not glue |

**Conclusion**: glue is NOT the bottleneck. All 100 produce executable SQL.
All 100 produce wrong SQL. The bottleneck is model quality + missing NatSQL
v0.3 features. This validates the Phase B+C+D priority ordering.

## Pre-Phase-B fixes applied during Phase A

The following bugs were found and fixed while establishing the baseline.
They improve executability (bail\_rate 100 % → 0 %) but not semantic accuracy.

| Fix | Effect |
|---|---|
| Field vocabulary not registered in graph | Stage 1 linker had zero field candidates; fixed in `semsql-cli/extract.rs` |
| Stale graph cache (no field vocab) | Deleted and rebuilt; fresh graphs have 89 field entries for `california_schools` |
| Degenerate skeleton (no FROM) | Added fallback: `SELECT COUNT(*) FROM @entity1` or `SELECT * FROM @entity1` |
| Escalation always fatal | Made non-fatal; Stage 4 fallback chain handles partial fills |
| NatSQL parse error always fatal | Added cascading fallback: validate → bare parse → strip WHERE → minimal SELECT |
| Cross-entity field references | `fix_from_entity_mismatch()` swaps FROM entity to match most-common field prefix |
| Canonical snake\_case vs DB column names with spaces | `rewrite_db_columns()` + `field_db_column_map()` rewrite `county_name` → `` `County Name` `` |
| `top_k_entities: 5, top_k_fields: 10` OOD | Reduced to 2/4 matching Spider training distribution |
| ABSTAIN\_THRESHOLD = 0.4 | Lowered to 0.1 for BIRD domain (scores 0.18-0.38 on BIRD) |

## Next steps

1. **Phase B** — NatSQL v0.3: add JOIN chains, HAVING, arithmetic to AST +
   parser + transpiler + grammar (eliminates 71 + 20 = ~80 % of root causes).
2. **Phase C** — OmniSQL training data ingest (BIRD in-distribution data).
3. **Phase D** — Retrain Stage 2 + Stage 3 on OmniSQL; expect EX to jump
   from 0 % to the 25–35 % range the plan estimates.
4. **Phase E** — llguidance constraint (eliminates hallucinated field names,
   expected +3–5 pts EX).

# Phase D iteration — cascade-v3.x BIRD-100 progression

| Cascade | Stage 2 | Stage 1 | Stage 3 | Extractor | exec_acc | bail_rate |
|---|---|---|---|---|---|---|
| v2 baseline | t5-small (Spider) | distilbert (Spider) | distilbert (Spider, 278 rows) | legacy | 0.0 % | 0 % |
| v3.0 | t5-small (50K v3, 5h CPU) | v2 (untouched) | v2 (untouched) | legacy | 0.0 % | 0 % |
| v3.1 | v3.0 | distilbert (67K linker, 1h CPU) | v2 | legacy | 0.0 % | 0 % |
| **v3.2** | v3.0 | v3.1 | distilbert (10K v3, 3.5h CPU) | legacy | **1.0 %** | 0 % |
| v3.3 | v3.0 | v3.1 | v3.2 | rich (gated) | 0.0 % | 0 % |
| v3.3b | v3.0 | v3.1 | v3.2 | legacy | 1.0 % | 0 % |

## What changed between baselines

**v2 → v3.0**: Stage 2 retrained on v3 ultimate corpus (498,980 rows /
179,383 INNER-JOIN-bearing skeletons / 179,212 FK rows). Two corpus
bugs found and fixed during this run:

  * `_SkeletonBuilder` was registering joined entities but stripping
    `INNER JOIN` syntax from the rendered skeleton — only 0–10 JOIN
    rows in the entire 500K teacher cache. Fixed in
    `python/semsql_train/src/semsql_train/teacher_cache.py` by
    emitting `INNER JOIN @entityN ON @fieldX = @fieldY` slots.
  * `export-cascade` left stale v2 `model_quantized.onnx` files in the
    target directories; ORT's quantiser then failed with
    `multi-file quantization not supported` and the v3 manifest
    pointed at the old quantised weights. Fixed by deleting the old
    `model_quantized.onnx` and `model.onnx` siblings before re-export.

**v3.0 → v3.2**: linker + slot-filler retrained on v3 corpora. The
slot-filler corpus jumped from 278 rows to 10K via
`derive-slot-pairs` — each Stage 2 row's `slot_map` becomes one
Stage 3 record, with synthesised candidate sets that include
hard-negative NL stop-words (`'highest'`, `'students'`, …) so the
cross-encoder learns to rank them below numerics + capitalised
content. The v3.2 cascade now picks numerics + dates (`'400'`,
`'500'`, `'5-17'`, `'K-12'`, `'2000/1/1'`) where v3.0 picked
stop-words (`'highest'`, `'phone'`, `'opened'`).

**v3.3 vs v3.3b — extractor coupling**: A richer NL candidate
extractor (`extract_nl_value_candidates_rich`) was added to surface
multi-word capitalised phrases (`'Fresno County Office of Education'`),
quoted strings, and ISO dates. 57 of 100 BIRD predictions changed
relative to v3.2, with net wins on phrase-bearing questions but
regressions on numeric WHERE comparisons (`'400'` → `'Math'`,
`'500'` → `'SAT'`) — the v3.2 cross-encoder was trained on a
simpler candidate distribution and ranks proper-noun acronyms above
numerics in the new richer pool. The rich extractor is gated until
Stage 3 is retrained against the matching candidate distribution
(re-run `derive-slot-pairs` with the rich-extractor logic baked in,
then bump cascade version on re-export).

## Outstanding bottlenecks (post-v3.2)

| Failure class | Driver | Fix path |
|---|---|---|
| Stage 1 wrong field rank | Linker corpus is single-item ranking; multi-table queries demand multi-entity rankings | Generate linker pairs from teacher-cache `ranked_schema` rows so each row teaches multi-entity disambiguation |
| Stage 2 still single-FROM on hard JOINs | t5-small under-fits 50K balanced subset on 1 epoch CPU | t5-base × 5 epochs × full 500K (Phase D GPU step) |
| Stage 3 picks wrong field for comparison | Cross-encoder ties acronyms with numerics | Retrain on richer-extractor candidate distribution OR add per-slot type bias (numeric > word for `>` / `<` / `BETWEEN`) |
| Slot fill collapses to single-token even when phrase needed | Legacy extractor capped at 30 single-token candidates | Re-enable rich extractor + retrain Stage 3 on matching distribution |

## Operator handoff (Phase D GPU step)

The codebase, corpus, and cascade are reproducible end-to-end with
the artefacts under `data/skeleton_train_v3_*.jsonl` and
`target/cascade-v3/`. Closing the gap from 1 % to the 25–35 % Phase D
target requires GPU retraining:

1. **Stage 2 t5-base × 5 epochs × full 500K**: ~3–4 days RTX 4060.
   `python -m semsql_train train --stage skeleton --base-model t5-base --train data/skeleton_train_v3_ultimate.jsonl --epochs 5 --batch-size 4 --grad-accum 32 --bf16 --out target/skeleton-v3-base`.
2. **Stage 3 distilbert × 3 epochs × 95K** (after re-deriving with
   `derive-slot-pairs --max-rows 200000`): ~30–60 min RTX 4060.
3. **Stage 1 distilbert × 3 epochs × 67K** with multi-entity ranking
   pairs derived from the v3 teacher cache.
4. Re-export cascade-v3-base + run BIRD-dev full (1534 examples).
   Acceptance gate: BIRD EX ≥ 35 % per `docs/completion-plan.md`.
