# Provider Metric Shape Contract v2

Date: 2026-06-06. Retained proof for provider-style typed metric plans over a
non-sensitive CRM SemanticGraph.

## Result

PASS. Three metric-catalog-backed packets rendered locally with matching result
shapes and no direct provider SQL.

## Evidence

- packet/proposal batch: `target\v02\provider-metric-shape-contract-v2`
- requests previewed: `3`, provider calls: `0`
- strict local renders: `3/3`
- shape matches: `3/3`
- invalid renders: `0`
- result shapes: `scalar_metric`, `categorical_chart`, `multi_series_chart`
- distinct SQL: `COUNT(DISTINCT "leads"."account_id")`

## Cases

- `crm-lead-conversion-rate`: conditional rate over backed `status=converted`.
- `crm-average-deal-value-by-channel`: `AVG(deal_value)` grouped by channel.
- `crm-unique-accounts-by-date-channel`: distinct-count metric grouped by date
  and channel.

## Limits

This is a provider-style contract replay, not a live-provider quality score.
Earlier fraudv transaction route proposals failed closed under the sensitive
schema policy, which is expected and should not be weakened for metric demos.
