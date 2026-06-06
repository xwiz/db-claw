# Real App Framework Metric Probe: fraudv v3

Date: 2026-06-06. Retained proof for real-app authored rate, aggregate, and
distinct-count metric ingestion.

## Result

PASS. A real Laravel app plus local MariaDB schema accepted three authored
metrics and exposed all three in rejected-query packets as bounded
`metric_catalog_hits`.

## Evidence

- app: `C:\Users\Son\cowork\fraudv`
- framework/database: `laravel` / `fraudv_go`
- graph: `target\v02\real-app-framework-fraudv-metrics-v3\app.framework.semsql`
- raw source fragments: `1262`
- source vocab grounded: `288/288`
- entities/fields/relationships: `29/421/38`
- metric definitions: `3`
- metric packet checks: `3/3`
- source-entity query checks: `5/5`, required `3`
- sample-value rows: `0`
- artifacts: `target\v02\real-app-framework-fraudv-metrics-v3\report.json`,
  `target\v02\real-app-framework-fraudv-metrics-v3\report.md`

## Change Proven

- `high_score_transaction_rate` resolves singular `transaction.*` refs to
  DB-grounded `transactions.*` fields.
- `average_transaction_score` resolves `transaction.score` as an `AVG`
  aggregate measure.
- `unique_transaction_customers` resolves `transaction.bank_customer_id` as
  `COUNT(DISTINCT ...)`; the packet carries `distinct: true` and
  `measure_field: transactions.bank_customer_id`.

## Limits

This proves governed metric catalog plumbing on one real app. It does not
execute metric SQL or prove arbitrary metric semantics.
