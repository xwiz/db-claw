# Real DB MySQL/MariaDB Typed Fallback Probe

- status: `PASS`
- database: `fraud_radar`
- graph: `target\realdb_typed_fallback_mariadb_grouped_metrics_v1\graphs\fraud_radar.schemaonly.semsql`
- provider: `openai`
- families: `grouped_avg`
- high-risk schema: `False`
- safety mode: `schema-only extraction; no sample values; provider may propose typed plans only; SQL is locally rendered/validated and executed read-only with row values discarded`

## Summary

- questions: `3`
- selected SQL: `3/3`
- typed fallback selected: `0/3`
- local selected: `3`
- provider calls: `0`
- provider errors: `0`
- render errors: `0`
- execution ok: `3/3`
- expected table/field matches: `3/3`
- expected kinds: `{'grouped_avg': 3}`
- rows retained cases: `0`
- sample-value rows: `0`

## Records

| # | Question | Expected | Source | Provider Calls | Render | Exec | Rows Retained | Expected Match | OK | SQL |
|---:|---|---|---|---:|---|---|---|---|---|---|
| 1 | `which final status has the highest average payout amount for fraud reports` | `fraud_reports.payout_amount by fraud_reports.final_status` | `local` | `0` | `None` | `ok` | `False` | `True` | `True` | <code>SELECT `fraud_reports`.`final_status`, AVG(`fraud_reports`.`payout_amount`) AS `avg_payout_amount` FROM `fraud_reports` WHERE `fraud_reports`.`payout_amount` IS NOT NULL GROUP BY `fraud_reports`.`final_status` ORDER BY `avg_payout_amount` DESC LIMIT 1</code> |
| 2 | `which relationship type has the highest average amount for fraud reports` | `fraud_reports.amount by fraud_reports.relationship_type` | `local` | `0` | `None` | `ok` | `False` | `True` | `True` | <code>SELECT `fraud_reports`.`relationship_type`, AVG(`fraud_reports`.`amount`) AS `avg_amount` FROM `fraud_reports` WHERE `fraud_reports`.`amount` IS NOT NULL GROUP BY `fraud_reports`.`relationship_type` ORDER BY `avg_amount` DESC LIMIT 1</code> |
| 3 | `which result has the highest average recovered amount for entity fraud reports` | `entity_fraud_reports.recovered_amount by entity_fraud_reports.result` | `local` | `0` | `None` | `ok` | `False` | `True` | `True` | <code>SELECT `entity_fraud_reports`.`result`, AVG(`entity_fraud_reports`.`recovered_amount`) AS `avg_recovered_amount` FROM `entity_fraud_reports` WHERE `entity_fraud_reports`.`recovered_amount` IS NOT NULL GROUP BY `entity_fraud_reports`.`result` ORDER BY `avg_recovered_amount` DESC LIMIT 1</code> |
