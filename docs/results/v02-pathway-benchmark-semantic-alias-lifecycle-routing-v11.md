# v0.2 Semantic-Alias Lifecycle Routing v11

Date: 2026-06-05

Retained benchmark summary for generic schema/vocabulary fixes through the
semantic-alias pressure suites. Current gate numbers live in
`v02-evidence-ledger.md`.

## Change

Two generic improvements landed after v9:

- date projection/order selection now uses schema vocabulary, so phrases like
  "renewals" can select a compatible renewal date instead of dropping the
  requested projection;
- lifecycle status/date predicates can route through a related lifecycle/event
  table when FK, status-value, and lifecycle-date evidence support it.

Neither change is a query-specific route.

## Result

| Suite | v11 | Wrong accepted SQL |
|---|---:|---:|
| Platform semantic aliases | `11/11` | `0` |
| BI semantic aliases | `19/20` | `0` |

Recovered since v9:

- `ba010`, `support_renewal_intersection`;
- `ba012`, `churn_list_projection`.

Remaining BI semantic-alias false negative:

- `ba020`, `ratio_by_joined_dimension`: `SLA breach rate by segment`.

## Artifacts

- `target/v02/pathway-platform-semantic-alias-lifecycle-routing-v11/pathway_benchmark.md`
- `target/v02/pathway-platform-semantic-alias-lifecycle-routing-v11/pathway_benchmark.json`
- `target/v02/pathway-business-semantic-alias-lifecycle-routing-v11/pathway_benchmark.md`
- `target/v02/pathway-business-semantic-alias-lifecycle-routing-v11/pathway_benchmark.json`

## Verification

- `cargo test -p semsql-runtime graph_schema_atlas_tests -- --nocapture`
- `cargo build -p semsql-cli`
- `uv run python -m semsql_eval pathway-benchmark --suite platform --schema-variant semantic_alias --semsql-bin target/debug/semsql.exe --out target/v02/pathway-platform-semantic-alias-lifecycle-routing-v11`
- `uv run python -m semsql_eval pathway-benchmark --suite business --schema-variant semantic_alias --semsql-bin target/debug/semsql.exe --out target/v02/pathway-business-semantic-alias-lifecycle-routing-v11`
