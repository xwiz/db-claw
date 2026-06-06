# v0.2 Semantic-Alias Schema-Vocabulary Shadowing v5

Date: 2026-06-05

Retained benchmark summary for the generic schema-vocabulary shadowing fix.
Current gate numbers live in `v02-evidence-ledger.md`.

## Change

Sample/value-derived predicates are now suppressed when the candidate value is
also a schema vocabulary term for another entity, unless the target field/entity
is locally qualified next to that value.

Example: `NPS` in `Average NPS score by industry` names the survey/NPS metric
context. It should not become `region_name = 'NPS'` just because `NPS` appears
as a sampled region value. A prompt like `region NPS` can still use the value.

This is a generic atlas/value disambiguation rule, not a benchmark phrase map.

## Result

| Suite | Previous | v5 | Wrong accepted SQL |
|---|---:|---:|---:|
| Platform semantic aliases | `11/11` | `11/11` | `0` |
| BI semantic aliases | `15/20` | `16/20` | `0` |

Recovered case:

- `ba011`, `nps_group_avg`: `Average NPS score by industry in March 2024`

Remaining BI semantic-alias false negatives still fail closed:

- `support_renewal_intersection`;
- `churn_list_projection`;
- `inactive_owner_filter`;
- `ratio_by_joined_dimension`.

## Artifacts

- `target/v02/pathway-platform-semantic-alias-schema-vocab-shadow-v5/report.md`
- `target/v02/pathway-platform-semantic-alias-schema-vocab-shadow-v5/report.json`
- `target/v02/pathway-business-semantic-alias-schema-vocab-shadow-v5/report.md`
- `target/v02/pathway-business-semantic-alias-schema-vocab-shadow-v5/report.json`

## Verification

- `cargo test -p semsql-runtime graph_schema_atlas_tests -- --nocapture`
- `cargo clippy -p semsql-runtime --all-targets --features onnx -- -D warnings`
- `cargo build -p semsql-cli`
- `uv run python -m semsql_eval pathway-benchmark --suite platform --schema-variant semantic_alias --semsql-bin target/debug/semsql.exe ...`
- `uv run python -m semsql_eval pathway-benchmark --suite business --schema-variant semantic_alias --semsql-bin target/debug/semsql.exe ...`
