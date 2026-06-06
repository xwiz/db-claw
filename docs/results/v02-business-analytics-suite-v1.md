# Business Analytics NL-to-SQL Suite

Retained suite spec for `business-analytics-v1`; current readiness numbers live
in [v02-evidence-ledger.md](v02-evidence-ledger.md).

## Fixture

- DB: `business_analytics`
- SQLite: `target/business_analytics_suite_v1/database/business_analytics/business_analytics.sqlite`
- URI: `sqlite:///target/business_analytics_suite_v1/database/business_analytics/business_analytics.sqlite`

## Coverage Contract

The suite stresses BI dashboards, customer analytics, CRM pipeline, growth,
renewals, sales ops, support ops, and metric safety boundaries.

| Disposition | Cases | Purpose |
|---|---:|---|
| route | `ba001`-`ba020` | executable read-only SQL for governed BI/ops shapes |
| clarify | `ba021`-`ba023` | ambiguous or undefined metrics: revenue, pipeline, risk |
| reject | `ba024`-`ba026` | PII row dump, write-side effect, causal analysis |

Route families to preserve: owner projection, grouped ARR/MRR/NPS/support
metrics, CRM top-k pipeline, growth channel/campaign counts, sales-cycle
duration, overdue billing, support-renewal intersection, domain lookup,
inactive-owner filter, daily signups, stage sums, temporal anti-join, conversion
rate, and SLA breach rate.

## Use

Ask each question from `questions.jsonl`; compare route cases to
`expected.sql`; count clarify and reject cases separately. A pass here does not
prove arbitrary NL-to-SQL readiness and must not override the current status
card.
