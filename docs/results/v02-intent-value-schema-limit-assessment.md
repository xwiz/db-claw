# v0.2 Intent, Value, And Schema Limit Assessment

Date: 2026-06-06. Retained decision memo; current numbers live in
[v02-evidence-ledger.md](v02-evidence-ledger.md).

## Decision

Raw schema names plus first-N samples are not enough for production-grade
NL-to-SQL. The product path is:

`SemanticGraph -> SemanticAtlas -> source-spanned IntentFrame -> typed
BoundQueryPlan -> local renderer/validator -> optional typed LLM proposal`.

This is credible for governed BI, CRM, growth, sales, support, and operational
read-only questions when the graph has labels, relationships, field-scoped
values, active-table hints, and metric definitions. Opaque databases with
undefined metrics must clarify or reject.

## Evidence To Preserve

- Binder probes found proof-ready graph evidence in `47.00%` of random BIRD dev
  and `57.50%` of livepath70 mismatches.
- Join-path evidence was strong: `84.43%` on random BIRD and `100.00%` on the
  governed practical route-suite probes.
- Governed product pathway: `31/31` with `0` wrong accepted SQL.
- Older oracle-plan and pre-BoundQueryPlan numbers are historical only.

## Deterministic Scope

Atlas/planner logic should own noun/value extraction, intent shape, typed
literals, field-compatible values, join planning, date windows, metric routing,
and result-shape hints. Stage 3 may rank legal candidates only; it must not plan
SQL globally.

## Active Gaps

- Sampled values and source vocabulary are now bounded packet evidence for
  exact/unique field and enum-value hits.
- Durable graph metric definitions and unique packet-backed conditional-rate
  formulas are now bounded metric evidence; ambiguous candidates still require
  clarification.
- Date windows now use unique date-role anchors while preserving fail-closed
  ambiguity when no role term disambiguates the date field.
- Keep improving value-level tenant/account/org scoping.
- Probe real-app metric definitions, then add governed catalog definitions for
  churn, health, risk, SLA breach, pipeline, retention, NPS, and
  organization-specific rates.
- Collapse specialized QueryFrame routes into one `IntentFrame ->
  BoundQueryPlan` boundary.

## Operating Expectation

| Evidence level | Fair expectation |
|---|---|
| Raw schema plus shallow samples | useful local wins, not 90% |
| Field-scoped values plus aliases | strong filters, lookups, counts, top-k, simple grouped aggregates |
| Metric atlas plus typed fallback | credible high-accuracy BI/ops target |
| Undefined metrics, causal analysis, writes, row dumps | clarify or reject |

## Next

Real-app metric probes -> broader real-app alias probes -> typed fallback
packets for rejected cases. Track recall, coverage, accepted SQL accuracy, and
wrong accepted SQL.
