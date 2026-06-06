# Real DB MySQL/MariaDB Typed Fallback Probe

- status: `PASS`
- database: `fraud_radar`
- graph: `target\realdb_typed_fallback_mariadb_joined_filtered_grouped_metrics_v1\graphs\fraud_radar.schemaonly.semsql`
- provider: `none`
- families: `joined_filtered_grouped_avg`
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
- expected kinds: `{'joined_filtered_grouped_avg': 3}`
- rows retained cases: `0`
- sample-value rows: `0`

## Records

| # | Question | Expected | Source | Provider Calls | Render | Exec | Rows Retained | Expected Match | OK | SQL |
|---:|---|---|---|---:|---|---|---|---|---|---|
| 1 | `which fraud reports fraud type has the highest average recovered amount for entity fraud reports` | `entity_fraud_reports.recovered_amount by fraud_reports.fraud_type via entity_fraud_reports.fraud_report_id = fraud_reports.id` | `local` | `0` | `None` | `ok` | `False` | `True` | `True` | <code>SELECT `fraud_reports`.`fraud_type`, AVG(`entity_fraud_reports`.`recovered_amount`) AS `avg_recovered_amount` FROM `entity_fraud_reports` INNER JOIN `fraud_reports` ON `entity_fraud_reports`.`fraud_report_id` = `fraud_reports`.`id` WHERE `entity_fraud_reports`.`recovered_amount` IS NOT NULL GROUP BY `fraud_reports`.`fraud_type` ORDER BY `avg_recovered_amount` DESC LIMIT 1</code> |
| 2 | `which entities type has the highest average amount for fraud reports that have police report` | `fraud_reports.amount by entities.type where fraud_reports.has_police_report = 1 via fraud_reports.entity_id = entities.id` | `local` | `0` | `None` | `ok` | `False` | `True` | `True` | <code>SELECT `entities`.`type`, AVG(`fraud_reports`.`amount`) AS `avg_amount` FROM `fraud_reports` INNER JOIN `entities` ON `fraud_reports`.`entity_id` = `entities`.`id` WHERE `fraud_reports`.`has_police_report` = 1 AND `fraud_reports`.`amount` IS NOT NULL GROUP BY `entities`.`type` ORDER BY `avg_amount` DESC LIMIT 1</code> |
| 3 | `which entities name has the highest average allocated amount for entity fraud reports` | `entity_fraud_reports.allocated_amount by entities.name via entity_fraud_reports.entity_id = entities.id` | `local` | `0` | `None` | `ok` | `False` | `True` | `True` | <code>SELECT `entities`.`name`, AVG(`entity_fraud_reports`.`allocated_amount`) AS `avg_allocated_amount` FROM `entity_fraud_reports` INNER JOIN `entities` ON `entity_fraud_reports`.`entity_id` = `entities`.`id` WHERE `entity_fraud_reports`.`allocated_amount` IS NOT NULL GROUP BY `entities`.`name` ORDER BY `avg_allocated_amount` DESC LIMIT 1</code> |
