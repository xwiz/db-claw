# Business Random-Alias Benchmark Seed 42 v7

Date: 2026-06-05

Status: historical random-physical-name pressure test. Superseded by
`v02-pathway-benchmark-random-alias-seed42-v16.md` and breadth evidence in
`v02-pathway-benchmark-random-alias-breadth-v1.md`.

## Command

```powershell
uv run python -m semsql_eval pathway-benchmark `
  --suite business `
  --schema-variant random_alias `
  --schema-alias-seed 42 `
  --semsql-bin target\debug\semsql.exe `
  --out target\v02\pathway-business-random-alias-seed42-v7
```

## Signal

| Policy | Route correct | Wrong accepted SQL | Route fail-closed | Non-route fail-closed |
|---|---:|---:|---:|---:|
| current permissive | `12/20` | `0` | `8` | `6/6` |
| frame only | `12/20` | `0` | `8` | `6/6` |
| bounded Stage 3 | `12/20` | `0` | `8` | `6/6` |
| BoundQueryPlan | `12/20` | `0` | `8` | `6/6` |

Raw output:

- `target/v02/pathway-business-random-alias-seed42-v7/pathway_benchmark.json`
- `target/v02/pathway-business-random-alias-seed42-v7/pathway_benchmark.md`

## What Changed

Coverage improved from seed-42 v3 `6/20` to `12/20` while preserving `0`
wrong accepted SQL. The generic fixes were:

- entity-adjacent measure anchoring, for example `invoice amount`;
- bounded multi-hop entity display dimensions, for example `account owner`;
- default entity display ranking that prefers `name`/`title`/`label` fields.

## Remaining Gap

The 8 fail-closed route families are `anti_join_temporal`,
`churn_list_projection`, `crm_pipeline_topk`, `inactive_owner_filter`,
`pipeline_stage_group_sum`, `renewal_owner_projection`,
`structured_domain_lookup`, and `support_renewal_intersection`.

Next recovery should use atlas evidence and typed fallback packets, not weaker
promotion rules.
