# v0.2 Semantic-Alias Lifecycle Guard v9

Date: 2026-06-05

Retained benchmark summary for the generic lifecycle/event-table ambiguity
guard. Current gate numbers live in `v02-evidence-ledger.md`.

## Change

Semantic-alias value shadowing is kept, but lifecycle shortcuts now fail closed
when a base entity status/date plan conflicts with related lifecycle evidence.

Example shape: `churned accounts in Q1` must not silently become
`accounts.status = churned` plus `accounts.created_date`. If a related
subscription/contract/event table has equivalent lifecycle status evidence
(`cancelled`, `ended`, `terminated`, etc.) and an end/cancel date, SemSQL
rejects the incomplete plan until the related event table can be selected.

This is schema/value evidence, not a query-specific route.

## Result

| Suite | v9 | Wrong accepted SQL |
|---|---:|---:|
| Platform semantic aliases | `11/11` | `0` |
| BI semantic aliases | `17/20` | `0` |

Recovered safety:

- `ba012`, `churn_list_projection`: now fails closed instead of accepting the
  base-entity `created_date` shortcut.

Remaining BI semantic-alias false negatives:

- `support_renewal_intersection`;
- `churn_list_projection`;
- `ratio_by_joined_dimension`.

## Artifacts

- `target/v02/pathway-platform-semantic-alias-lifecycle-guard-v9/pathway_benchmark.md`
- `target/v02/pathway-platform-semantic-alias-lifecycle-guard-v9/pathway_benchmark.json`
- `target/v02/pathway-business-semantic-alias-lifecycle-guard-v9/pathway_benchmark.md`
- `target/v02/pathway-business-semantic-alias-lifecycle-guard-v9/pathway_benchmark.json`

## Verification

- `cargo test -p semsql-runtime graph_schema_atlas_tests -- --nocapture`
- `cargo build -p semsql-cli`
- `uv run python -m semsql_eval pathway-benchmark --suite platform --schema-variant semantic_alias --semsql-bin target/debug/semsql.exe --out target/v02/pathway-platform-semantic-alias-lifecycle-guard-v9`
- `uv run python -m semsql_eval pathway-benchmark --suite business --schema-variant semantic_alias --semsql-bin target/debug/semsql.exe --out target/v02/pathway-business-semantic-alias-lifecycle-guard-v9`
