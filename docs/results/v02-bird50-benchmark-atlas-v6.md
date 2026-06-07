# v0.2 BIRD50 Benchmark Atlas v6
Date: 2026-06-07. Retained one-run benchmark evidence; live status remains in
[v02-current-status.md](v02-current-status.md).

## Scope
Fresh first-50 BIRD dev rerun after cross-slot BoundQueryPlan gates for:

- explicit numeric field phrases;
- requested row/date projections;
- numeric range predicates;
- actor-filter phrases;
- role-word value misuse;
- date-role mismatch;
- superlative order-field mismatch;
- ratio-of-groups shape mismatch.

Artifacts:

- report: `target/v02/current-bird50-atlas-gated-v6/report.json`
- diagnosis: `target/v02/current-bird50-atlas-gated-v6/diagnosis.md`
- manifest: `target/v02/cascade-v3-runtime-covered500-adapt/manifest.json`

## Result
- total: `50`
- correct: `3`
- exec_acc: `6.00%`
- final SQL emitted: `3`
- final SQL wrong: `0`
- route-used wrong SQL: `0`
- model SQL after route reject: `0`

This is a product-safety improvement, not benchmark readiness. Earlier atlas
checkpoints still emitted accepted-wrong SQL; this run fails closed on the
previous accepted-wrong first-50 routes.

## Remaining Work
The dominant buckets are now `missing_onnx_feature`, `schema_linker_or_join_planning`,
`slot_value_grounding`, and `skeleton_planning`. Next progress should improve
DB-only atlas/codebook coverage and typed fallback/model resolution so fail-closed
cases become grounded plans, without reintroducing static shortcuts.
