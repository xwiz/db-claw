# Real App Framework Metric Probe: fraudv v1

Date: 2026-06-06. Retained proof for real-app authored metric ingestion.

## Result

PASS. A real Laravel app plus local MariaDB schema accepted an authored
conditional-rate metric, stored it in the graph, and exposed it in a rejected
query packet as `metric_catalog_hits`.

## Evidence

- app: `C:\Users\Son\cowork\fraudv`
- framework/database: `laravel` / `fraudv_go`
- graph: `target\v02\real-app-framework-fraudv-metrics-v1\app.framework.semsql`
- raw source fragments: `1262`
- source vocab grounded: `288/288`
- entities/fields/relationships: `29/421/38`
- metric definitions: `1`
- metric packet checks: `1/1`
- source-entity query checks: `5/5`, required `3`
- sample-value rows: `0`
- artifacts: `target\v02\real-app-framework-fraudv-metrics-v1\report.json`,
  `target\v02\real-app-framework-fraudv-metrics-v1\report.md`

## Change Proven

- Authored metric `high_score_transaction_rate` resolved singular
  `transaction.*` refs to DB-grounded `transactions.*` fields.
- Rejected query `high score transaction rate` produced a bounded packet with
  the metric catalog hit; no direct LLM SQL or query-specific route was used.
- JSONL ingest now tolerates UTF-8 BOMs from shell/editor-created files.

## Limits

This proves authored metric graph and packet plumbing on one real app. It does
not execute the metric SQL or prove arbitrary metric semantics.
