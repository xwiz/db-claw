# v0.2 BIRD50 Benchmark Atlas v2
Date: 2026-06-07. Retained one-run benchmark evidence; live status remains in
[v02-current-status.md](v02-current-status.md).

## Scope
Bounded rerun of the first 50 BIRD dev examples after typed
`missing_value_evidence` packets, entity-role `No.` id aliases, and
selected-measure threshold evidence.

Artifacts:

- report: `target/v02/current-bird50-atlas-gated-v2/report.json`
- diagnosis: `target/v02/current-bird50-atlas-gated-v2/diagnosis.md`
- manifest: `target/v02/cascade-v3-runtime-covered500-adapt/manifest.json`

## Result
- total: `50`
- correct: `3`
- exec_acc: `6.00%`
- final SQL emitted: `10`
- final SQL wrong: `7`
- route-used wrong SQL: `7`
- model SQL after route reject: `0`

Compared with the previous atlas-gated first-50 checkpoint, final SQL emissions
dropped from `28` to `10` and accepted wrong SQL dropped from `26` to `7`.
That is a safety improvement, not a benchmark-readiness claim.

## Remaining Accepted-Wrong Routes
Indexes `20`, `29`, `33`, `35`, `45`, `47`, and `48` remain route-used wrong.
They pressure grade-span fields, top-by-metric routing, enrollment/free-meal
range parsing, charter/fewest-students ranking, owner-scoped grouped averages,
monthly aggregates, and ratio shapes.

## Decision
Do not create BIRD-shaped tables or static example mappings. Continue with
DB-only SemanticAtlas/codebook enrichment plus generic metric/rank/grade-span
evidence gates, then rerun BIRD100 with accepted wrong SQL as the first stoplight.
