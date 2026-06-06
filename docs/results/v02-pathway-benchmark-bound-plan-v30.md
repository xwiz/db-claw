# Pathway Benchmark Bound Plan v30

Status: retained practical-pathway gate evidence. Live status lives in
`v02-current-status.md`; live numbers live in `v02-evidence-ledger.md`.

## Command

```powershell
uv run python -m semsql_eval pathway-benchmark `
  --semsql-bin target\debug\semsql.exe `
  --out target\pathway_decision_benchmark_bound_plan_v30
```

## Signal

- benchmark: `pathway-decision-v1`
- schema variant: `canonical`
- cases: `44`
- route cases: `31`
- non-route cases: `13`
- policies checked: `current_permissive`, `frame_only`, `bounded_stage3`,
  `bound_plan`

| policy | route correct | wrong accepted SQL | non-route fail-closed |
|---|---:|---:|---:|
| current permissive | `31/31` | `0` | `13/13` |
| frame only | `31/31` | `0` | `13/13` |
| bounded Stage 3 | `31/31` | `0` | `13/13` |
| BoundQueryPlan | `31/31` | `0` | `13/13` |

Runtime notes: `stage3_sql_total = 0`, `frame_promoted_route_wrong_sql = 0`,
`bound_plan_route_wrong_sql = 0`.

## Read

The practical platform and BI route suite is green under the typed compiler
boundary. This is release evidence for the curated practical pathway, not proof
of arbitrary schema or benchmark readiness.

## Retained Detail

Case matrix and frame paths live in:

- `artifacts/results-json/docs-results/v02-pathway-benchmark-bound-plan-v30.json`
- `target/pathway_decision_benchmark_bound_plan_v30/`

Fresh post-random-key-safety verification:
`target/v02/pathway-canonical-after-random-key-safety-v32/`.

Fresh post-anchor/display verification:
`target/v02/pathway-canonical-after-anchor-display-v34/`.

Fresh post-identityguard verification:
`target/v02/pathway-canonical-after-identityguard-v35/`.

## Next Pressure

Broaden beyond curated names with semantic-alias and random-alias BI/CRM/growth
pressure tests. Promote only if wrong accepted SQL remains `0`.
