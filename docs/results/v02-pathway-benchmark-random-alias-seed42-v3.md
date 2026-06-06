# Business Random-Alias Benchmark Seed 42 v3

Date: 2026-06-05

Status: historical. Superseded by
`v02-pathway-benchmark-random-alias-seed42-v7.md`.

## Command

```powershell
uv run python -m semsql_eval pathway-benchmark `
  --suite business `
  --schema-variant random_alias `
  --schema-alias-seed 42 `
  --semsql-bin target\debug\semsql.exe `
  --out target\v02\pathway-business-random-alias-seed42-v3
```

## Signal

- route cases: `20`
- non-route cases: `6`
- route correct: `6/20`
- wrong accepted SQL: `0`
- route fail-closed: `14`
- non-route fail-closed: `6/6`
- non-route unexpected SQL: `0`

Raw output:

- `target/v02/pathway-business-random-alias-seed42-v3/pathway_benchmark.json`
- `target/v02/pathway-business-random-alias-seed42-v3/pathway_benchmark.md`

## What Changed From v1

Wrong accepted SQL dropped from `5` to `0` after grouped aggregate safety began
rejecting identity/FK/join-endpoint measures and dimensions under opaque
physical names.

## Remaining Gap

Coverage is still low. The `14` fail-closed route families include grouped ARR,
CRM pipeline top-k, billing owner sums, subscriptions/MRR, churn lists,
campaign/support intersections, ratio/grouped metrics, anti-join temporal, and
structured domain lookup.

## Next Fix

Recover coverage through SemanticAtlas evidence and typed fallback packets, not
by relaxing the key safety gate or adding query-specific shortcuts.
