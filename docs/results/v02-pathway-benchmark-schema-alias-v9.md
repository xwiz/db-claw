# Platform Schema-Alias Benchmark v9

Status: historical semantic-alias pressure test. Superseded by
`v02-pathway-benchmark-semantic-alias-boolean-sample-domain-v12.md`.

## Signal

- suite: `platform`
- schema variant: `semantic_alias`
- route cases: `11`
- non-route cases: `7`
- result: `9/11` route-correct, `0` wrong accepted SQL, `2` route
  fail-closed, `7/7` non-route fail-closed

## Lesson Kept

The runtime was safe under platform schema aliases but under-covered for
top-k/grouped aggregate cases. Later semantic-alias work closed this gap.

## Superseded By

Latest semantic-alias gate:
`v02-pathway-benchmark-semantic-alias-boolean-sample-domain-v12.md`.

Raw v9 JSON:
`artifacts/results-json/docs-results/v02-pathway-benchmark-schema-alias-v9.json`.
