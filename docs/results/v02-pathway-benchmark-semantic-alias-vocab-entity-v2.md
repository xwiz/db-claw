# Semantic-Alias Vocab Entity Routing Slice

Date: 2026-06-05

Retained implementation slice. Current rollups live in
[v02-evidence-ledger.md](v02-evidence-ledger.md).

## Change

Runtime QueryFrame routing now uses vocabulary-backed entity evidence in count
subject detection, grouped-count base entity selection, entity-before-`by`
selection, and related group dimension display selection.

It also suppresses cross-entity target-role filters in `A-to-B conversion rate`
phrases when the conversion numerator is already grounded, preventing
`customer` in `lead-to-customer` from becoming an extra organization filter.

## Results

Fresh `target/debug/semsql.exe` after `cargo build -p semsql-cli`.

| Suite | Before | After | Wrong accepted SQL |
|---|---:|---:|---:|
| Platform semantic aliases | `9/11` | `11/11` | `0` |
| Business semantic aliases | `9/20` | `14/20` | `0` |

Retained run artifacts:

- `target/v02/pathway-platform-semantic-alias-vocab-entity-v2/report.md`
- `target/v02/pathway-business-semantic-alias-vocab-entity-v2/report.md`

## Recovered Families

- platform `topk_group_count`
- platform `multi_join_group_avg`
- business `crm_pipeline_topk`
- business `growth_channel_group_count`
- business `campaign_conversion_count`
- business `support_rep_group_count`
- business `ratio_by_group`

## Remaining Business False Negatives

- `growth_channel_count`
- `support_renewal_intersection`
- `nps_group_avg`
- `churn_list_projection`
- `inactive_owner_filter`
- `ratio_by_joined_dimension`

## Verification

- `cargo fmt --check`
- `cargo test -p semsql-runtime semantic_alias_ -- --nocapture`: `5/5`
- `cargo test -p semsql-runtime graph_schema_atlas_tests -- --nocapture`: `90/90`
- `cargo build -p semsql-cli`
- `uv run python -m semsql_eval pathway-benchmark --suite platform --schema-variant semantic_alias ...`: `11/11`, `0` wrong SQL
- `uv run python -m semsql_eval pathway-benchmark --suite business --schema-variant semantic_alias ...`: `14/20`, `0` wrong SQL

## Next

The remaining misses are not count-subject alias misses. Next work should focus
on scalar count projection over date/value aliases, related-fact intersections,
average metric aliases, boolean owner filters, and joined-dimension ratios.
