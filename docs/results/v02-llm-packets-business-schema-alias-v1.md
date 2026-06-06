# Business Typed Fallback Packets

Compact retained report for schema-alias business packet capture.

## Result

- report: `artifacts/results-json/docs-results/v02-pathway-benchmark-business-schema-alias-v5.json`
- packet dir: `target/llm_resolution_packets/business_schema_alias_v5`
- suite DB: `target/pathway_decision_benchmark_business_schema_alias_v5/business/database/business_analytics_semantic_alias/business_analytics_semantic_alias.sqlite`
- packets: `11`
- provider used: `none`
- strict render validation: `11/11`
- execution-equivalent to expected SQL: `11/11`
- validation issues: `0`

## Packet Families

| Cases | Families |
|---|---|
| `ba003`, `ba004`, `ba005`, `ba006` | top-k pipeline, date-window counts, campaign conversion |
| `ba010`, `ba011`, `ba012`, `ba014` | intersections, grouped avg, churn projection, inactive owner filter |
| `ba017`, `ba019`, `ba020` | support grouping, conditional rates, joined-dimension rates |

## Boundary Proven

Rejected local packets can be converted into typed proposals, rendered locally,
validated strictly, and executed read-only without accepting direct model SQL.
This is fallback-boundary proof, not proof that live provider routing is the
default production path.
