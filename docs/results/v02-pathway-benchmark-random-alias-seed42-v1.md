# Business Random-Alias Benchmark Seed 42 v1

Date: 2026-06-05

Status: historical pre-fix diagnostic. Superseded by
`v02-pathway-benchmark-random-alias-seed42-v3.md`.

## Command

```powershell
uv run python -m semsql_eval pathway-benchmark `
  --suite business `
  --schema-variant random_alias `
  --schema-alias-seed 42 `
  --semsql-bin target\debug\semsql.exe `
  --out target\v02\pathway-business-random-alias-seed42-v1
```

## Signal

- route cases: `20`
- non-route cases: `6`
- route correct: `6/20`
- wrong accepted SQL: `5`
- route fail-closed: `9`
- non-route fail-closed: `6/6`
- non-route unexpected SQL: `0`

Raw output:

- `target/v02/pathway-business-random-alias-seed42-v1/pathway_benchmark.json`
- `target/v02/pathway-business-random-alias-seed42-v1/pathway_benchmark.md`

## Wrong Accepted Families

- `grouped_arr_metric`
- `crm_pipeline_topk`
- `subscription_mrr_group`
- `billing_owner_group_sum`
- `inactive_owner_filter`

## Read

The curated semantic-alias gates prove vocabulary aliases work when the schema
still carries enough semantic structure. Random physical names expose a deeper
safety gap: grouped aggregate routing can choose identity/FK fields as measures
or dimensions when column names are opaque.

## Fixed Afterward

`v3` keeps the same `6/20` route-correct coverage but drops wrong accepted SQL
from `5` to `0`.

## Original Next Fix

Make grouped aggregate acceptance relationship-aware:

- reject ID/FK/join-endpoint dimensions unless the user asks for IDs;
- reject ID/FK/join-endpoint numeric measures;
- fail closed for list-style prompts that accidentally enter grouped aggregate
  routing;
- rerun this same seed and require `0` wrong accepted SQL before promoting any
  random-alias result.
