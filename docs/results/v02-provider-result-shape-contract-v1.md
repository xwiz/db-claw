# Provider Result-Shape Contract v1

Date: 2026-06-06. Retained proof for typed-provider result-shape gating.

## Result

PASS. A BI/customer-analytics render batch required provider-style typed
proposals to render locally and match their declared result shape.

## Evidence

- packet/proposal batch: `target\v02\provider-result-shape-contract-v1`
- packets: `4`
- valid rendered plans: `3`
- invalid rendered plans: `1`
- shape matches: `3`
- shape mismatches: `1`
- missing declared shape: `0`
- result shapes: `categorical_chart: 2`, `time_series_chart: 1`,
  `multi_series_chart: 1`
- mismatch bucket: `result_shape_mismatch`

## Cases

- `crm-leads-by-channel`: categorical grouped count matched.
- `crm-leads-over-time`: time-series grouped count matched.
- `crm-value-by-channel-over-time`: multi-series average matched.
- `crm-bad-multiseries`: declared `multi_series_chart` but rendered as
  `categorical_chart`; SQL stayed `null`.

## Limit

This is a typed-plan contract run, not a live-provider quality score. It proves
local validation catches provider shape mistakes before SQL can be accepted.
