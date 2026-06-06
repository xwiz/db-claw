# Real App Framework Metric Probe: fraudv v2

Date: 2026-06-06. Retained proof for real-app authored rate and aggregate
metric ingestion.

## Result

PASS. A real Laravel app plus local MariaDB schema accepted two authored
metrics, stored both in the graph, and exposed both in rejected-query packets as
bounded `metric_catalog_hits`.

## Evidence

- app: `C:\Users\Son\cowork\fraudv`
- framework/database: `laravel` / `fraudv_go`
- graph: `target\v02\real-app-framework-fraudv-metrics-v2\app.framework.semsql`
- raw source fragments: `1262`
- source vocab grounded: `288/288`
- entities/fields/relationships: `29/421/38`
- metric definitions: `2`
- metric packet checks: `2/2`
- source-entity query checks: `5/5`, required `3`
- sample-value rows: `0`
- artifacts: `target\v02\real-app-framework-fraudv-metrics-v2\report.json`,
  `target\v02\real-app-framework-fraudv-metrics-v2\report.md`

## Change Proven

- `high_score_transaction_rate` still resolves singular
  `transaction.*` refs to DB-grounded `transactions.*` fields.
- `average_transaction_score` resolves `transaction.score` as an aggregate
  measure field with `AVG`.
- Both rejected queries produced bounded metric catalog hits; no query-specific
  Rust route or direct LLM SQL was used.

## Limits

This proves governed metric catalog plumbing for one conditional-rate metric
and one aggregate metric on one real app. It does not execute metric SQL or
prove arbitrary metric semantics.
