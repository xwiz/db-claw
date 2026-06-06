# Business Random-Alias Benchmark Seed 42 v16

Date: 2026-06-05

Retained random-physical-name pressure test. Current rollups live in
[v02-evidence-ledger.md](v02-evidence-ledger.md). Full matrix stays in
`target/v02/pathway-business-random-alias-seed42-subjectmod-v16`.

## Command

```powershell
uv run python -m semsql_eval pathway-benchmark `
  --suite business `
  --schema-variant random_alias `
  --schema-alias-seed 42 `
  --semsql-bin target\debug\semsql.exe `
  --out target\v02\pathway-business-random-alias-seed42-subjectmod-v16
```

## Signal

| Policy | Route correct | Wrong accepted SQL | Route fail-closed | Non-route fail-closed |
|---|---:|---:|---:|---:|
| current permissive | `20/20` | `0` | `0` | `6/6` |
| frame only | `20/20` | `0` | `0` | `6/6` |
| bounded Stage 3 | `20/20` | `0` | `0` | `6/6` |
| BoundQueryPlan | `20/20` | `0` | `0` | `6/6` |

## What Changed

Generic subject extraction now treats lifecycle/status words such as `active`,
`inactive`, and `churned` as modifiers, not subject entities. This fixes the
random-alias `ba012` failure where `churned accounts` drifted to an owner/rep
entity through `account owner` alias evidence.

The fix is covered by a random/opaque schema regression test and preserves the
anchor runs: canonical `31/31`, paraphrase `124/124`, platform semantic aliases
`11/11`, and BI semantic aliases `20/20`, all with `0` wrong accepted SQL.

## Next Risk

Seed `42` is green, but it is still one random-alias sample. Broaden seed
coverage and real app-schema probes before treating this as general physical
schema robustness.
