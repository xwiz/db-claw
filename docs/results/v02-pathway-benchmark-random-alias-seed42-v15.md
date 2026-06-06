# Business Random-Alias Benchmark Seed 42 v15

Date: 2026-06-05

Retained random-physical-name pressure test. Current rollups live in
[v02-evidence-ledger.md](v02-evidence-ledger.md). Full matrix stays in
`target/v02/pathway-business-random-alias-seed42-topkbyboundary-v15`.

## Command

```powershell
uv run python -m semsql_eval pathway-benchmark `
  --suite business `
  --schema-variant random_alias `
  --schema-alias-seed 42 `
  --semsql-bin target\debug\semsql.exe `
  --out target\v02\pathway-business-random-alias-seed42-topkbyboundary-v15
```

## Signal

| Policy | Route correct | Wrong accepted SQL | Route fail-closed | Non-route fail-closed |
|---|---:|---:|---:|---:|
| current permissive | `19/20` | `0` | `1` | `6/6` |
| frame only | `19/20` | `0` | `1` | `6/6` |
| bounded Stage 3 | `19/20` | `0` | `1` | `6/6` |
| BoundQueryPlan | `19/20` | `0` | `1` | `6/6` |

## What Changed

Generic top-k parsing now treats `by` as the boundary between requested group
entity and metric phrase, so `Top 3 reps by open pipeline...` groups by the
rep display field instead of drifting to a related attribute such as team.

This preserves the prior anchors: canonical `31/31`, paraphrase `124/124`,
platform semantic aliases `11/11`, and BI semantic aliases `20/20`, all with
`0` wrong accepted SQL.

## Remaining Gap

`ba012` (`churn_list_projection`) still fails closed with
`subject_entity_mismatch`: the route projects a related rep display field where
the subject should remain accounts. Fix through subject/projection evidence,
not by disabling the bound-plan safety gate.
