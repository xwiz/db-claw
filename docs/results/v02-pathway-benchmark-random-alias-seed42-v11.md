# Business Random-Alias Benchmark Seed 42 v11

Date: 2026-06-05

Historical random-physical-name pressure test. Use
[v02-evidence-ledger.md](v02-evidence-ledger.md) for current rollups.

## Command

```powershell
uv run python -m semsql_eval pathway-benchmark `
  --suite business `
  --schema-variant random_alias `
  --schema-alias-seed 42 `
  --semsql-bin target\debug\semsql.exe `
  --out target\v02\pathway-business-random-alias-seed42-identityguard-v11
```

## Signal

| Policy | Route correct | Wrong accepted SQL | Route fail-closed | Non-route fail-closed |
|---|---:|---:|---:|---:|
| current permissive | `15/20` | `0` | `5` | `6/6` |
| frame only | `15/20` | `0` | `5` | `6/6` |
| bounded Stage 3 | `15/20` | `0` | `5` | `6/6` |
| BoundQueryPlan | `15/20` | `0` | `5` | `6/6` |

Raw output:

- `target/v02/pathway-business-random-alias-seed42-identityguard-v11/pathway_benchmark.json`
- `target/v02/pathway-business-random-alias-seed42-identityguard-v11/pathway_benchmark.md`

## What Changed

Coverage improved from seed-42 v7 `12/20` to `15/20` while preserving `0`
wrong accepted SQL.

Generic fixes:

- default display scoring now uses vocabulary/display labels for real display
  fields;
- unsafe ID aliases require explicit ID wording;
- subject row identity can be the fallback projection only when no display
  field exists;
- FK/role identity columns no longer get display boosts from words such as
  customer, account, or owner.

## Remaining Gap

The 5 fail-closed route families are `renewal_owner_projection`,
`crm_pipeline_topk`, `churn_list_projection`, `inactive_owner_filter`, and
`pipeline_stage_group_sum`.

Next recovery should use atlas evidence and typed fallback packets, not weaker
promotion rules.
