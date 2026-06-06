# v0.2 Semantic-Alias Scope Suppression v4

Date: 2026-06-05

Retained benchmark summary for the generic temporal subject-descriptor fix.
Current gate numbers live in `v02-evidence-ledger.md`.

## Change

`new <entity alias>` is now treated as a temporal subject descriptor, not a
status predicate, when:

- the predicate value is `new`;
- the target field is status/state/stage-like;
- the prompt has a temporal window;
- the prompt does not explicitly mention status/state/stage;
- the entity phrase comes from schema/entity vocabulary.

This is generic alias/value grounding, not a static query fix.

## Result

| Suite | Previous | v4 | Wrong accepted SQL |
|---|---:|---:|---:|
| Platform semantic aliases | `11/11` | `11/11` | `0` |
| BI semantic aliases | `14/20` | `15/20` | `0` |

Recovered case:

- `ba004`, `growth_channel_count`: `How many new leads came from paid search in
  February 2024?`

Remaining BI semantic-alias false negatives still fail closed:

- `support_renewal_intersection`;
- `nps_group_avg`;
- `churn_list_projection`;
- `inactive_owner_filter`;
- `ratio_by_joined_dimension`.

## Artifacts

- `target/v02/pathway-platform-semantic-alias-scope-suppression-v4/report.md`
- `target/v02/pathway-platform-semantic-alias-scope-suppression-v4/report.json`
- `target/v02/pathway-business-semantic-alias-scope-suppression-v4/report.md`
- `target/v02/pathway-business-semantic-alias-scope-suppression-v4/report.json`

## Verification

- `cargo test -p semsql-runtime graph_schema_atlas_tests -- --nocapture`
- `cargo clippy -p semsql-runtime --all-targets --features onnx -- -D warnings`
- `cargo build -p semsql-cli`
- `uv run python -m semsql_eval pathway-benchmark --suite platform --schema-variant semantic_alias --semsql-bin target/debug/semsql.exe ...`
- `uv run python -m semsql_eval pathway-benchmark --suite business --schema-variant semantic_alias --semsql-bin target/debug/semsql.exe ...`
