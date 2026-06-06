# Pathway Benchmark After SemanticAtlas Assessment

Status: historical pre-BoundQueryPlan baseline. Do not use as the current
stoplight; use `v02-pathway-benchmark-bound-plan-v30.md`.

## Signal

- benchmark: `pathway-decision-v1`
- cases: `44`
- route cases: `30`
- non-route cases: `14`
- result: `3/30` route-correct, `16` wrong accepted SQL, `13/14`
  non-route fail-closed, `1` non-route unexpected SQL

This was the evidence that promoted frames were not safe enough. It justified
the BoundQueryPlan gate and the fail-closed policy before Stage 3/model repair.

## Useful Failure Families

Route wrong SQL clustered around multi-join projections, date ranges, grouped
metrics, top-k aggregates, boolean joins, pipeline metrics, NPS, and time
series. Non-route leakage exposed anti-join temporal handling as unsafe.

## Superseded By

- Current gate: `v02-pathway-benchmark-bound-plan-v30.md`
- Current dashboard: `v02-current-status.md`
- Raw output: `target/pathway_decision_benchmark_after_semantic_atlas_v1/report.json`

## Regression Rule

If a future change recreates any wrong accepted SQL family from this report,
prefer fail-closed typed packets over another route-local heuristic.
