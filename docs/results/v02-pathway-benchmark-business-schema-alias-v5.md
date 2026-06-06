# Business Schema-Alias Benchmark v5

Status: historical semantic-alias pressure test. Superseded by
`v02-pathway-benchmark-semantic-alias-boolean-sample-domain-v12.md`.

## Signal

- suite: `business`
- schema variant: `semantic_alias`
- route cases: `20`
- non-route cases: `6`
- result: `9/20` route-correct, `0` wrong accepted SQL, `11` route
  fail-closed, `6/6` non-route fail-closed

## Lesson Kept

The runtime was safe but under-covered on semantic aliases. False negatives were
mainly CRM pipeline, campaign conversion, support renewal, NPS, churn,
inactive-owner, and ratio/grouped cases.

## Superseded By

The v12 semantic-alias run reached:

- platform semantic aliases: `11/11`, `0` wrong accepted SQL
- BI semantic aliases: `20/20`, `0` wrong accepted SQL

Retained latest report:
`v02-pathway-benchmark-semantic-alias-boolean-sample-domain-v12.md`.

Raw v5 JSON:
`artifacts/results-json/docs-results/v02-pathway-benchmark-business-schema-alias-v5.json`.
