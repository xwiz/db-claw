# Live Provider OpenAI Metric Shape v1

Date: 2026-06-06. Retained proof for live typed-provider metric proposals over
bounded CRM packets.

## Result

PASS. OpenAI returned typed proposals for three metric/shape packets. SemSQL
validated and rendered all SQL locally; no provider SQL was accepted.

## Evidence

- packet/proposal batch: `target\v02\live-provider-openai-metric-shape-v1`
- provider calls: `3`
- provider errors: `0`
- strict local renders: `3/3`
- shape matches: `3/3`
- invalid renders: `0`
- result shapes: `scalar_metric`, `categorical_chart`, `multi_series_chart`
- rerendered distinct SQL: `COUNT(DISTINCT "leads"."account_id")`

## Cases

- `crm-lead-conversion-rate`: conditional rate from metric catalog evidence.
- `crm-average-deal-value-by-channel`: average metric grouped by channel.
- `crm-unique-accounts-by-date-channel`: distinct-count metric grouped by date
  and channel.

## Limits

This is a three-case live provider gate. It proves provider integration and
local validation for metric/shape packets, not broad arbitrary NL-to-SQL.
