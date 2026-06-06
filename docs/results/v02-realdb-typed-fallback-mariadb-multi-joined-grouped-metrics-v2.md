# Real DB MySQL/MariaDB Typed Fallback Probe

- status: `PASS`
- database: `fraud_radar`
- graph: `target\realdb_typed_fallback_mariadb_multi_joined_grouped_metrics_v2\graphs\fraud_radar.schemaonly.semsql`
- provider: `none`
- families: `multi_joined_filtered_grouped_avg`
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
- expected kinds: `{'multi_joined_filtered_grouped_avg': 3}`
- rows retained cases: `0`
- sample-value rows: `0`

## Records

| # | Question | Expected | Source | Provider Calls | Render | Exec | Rows Retained | Expected Match | OK | SQL |
|---:|---|---|---|---:|---|---|---|---|---|---|
| 1 | `which entities registration number has the highest average file size for fraud files that are verified` | `fraud_files.file_size by entities.registration_number where fraud_files.is_verified = 1 via fraud_files.fraud_report_id=fraud_reports.id -> fraud_reports.entity_id=entities.id` | `local` | `0` | `None` | `ok` | `False` | `True` | `True` | <code>SELECT `entities`.`registration_number`, AVG(`fraud_files`.`file_size`) AS `avg_file_size` FROM `fraud_files` INNER JOIN `fraud_reports` ON `fraud_files`.`fraud_report_id` = `fraud_reports`.`id` INNER JOIN `entities` ON `fraud_reports`.`entity_id` = `entities`.`id` WHERE `fraud_files`.`is_verified` = 1 AND `fraud_files`.`file_size` IS NOT NULL GROUP BY `entities`.`registration_number` ORDER BY `avg_file_size` DESC LIMIT 1</code> |
| 2 | `which entities type has the highest average file size for fraud files that are verified` | `fraud_files.file_size by entities.type where fraud_files.is_verified = 1 via fraud_files.fraud_report_id=fraud_reports.id -> fraud_reports.entity_id=entities.id` | `local` | `0` | `None` | `ok` | `False` | `True` | `True` | <code>SELECT `entities`.`type`, AVG(`fraud_files`.`file_size`) AS `avg_file_size` FROM `fraud_files` INNER JOIN `fraud_reports` ON `fraud_files`.`fraud_report_id` = `fraud_reports`.`id` INNER JOIN `entities` ON `fraud_reports`.`entity_id` = `entities`.`id` WHERE `fraud_files`.`is_verified` = 1 AND `fraud_files`.`file_size` IS NOT NULL GROUP BY `entities`.`type` ORDER BY `avg_file_size` DESC LIMIT 1</code> |
| 3 | `which banks code has the highest average allocated amount for entity fraud reports` | `entity_fraud_reports.allocated_amount by banks.code via entity_fraud_reports.entity_id=entities.id -> entities.bank_id=banks.id` | `local` | `0` | `None` | `ok` | `False` | `True` | `True` | <code>SELECT `banks`.`code`, AVG(`entity_fraud_reports`.`allocated_amount`) AS `avg_allocated_amount` FROM `entity_fraud_reports` INNER JOIN `entities` ON `entity_fraud_reports`.`entity_id` = `entities`.`id` INNER JOIN `banks` ON `entities`.`bank_id` = `banks`.`id` WHERE `entity_fraud_reports`.`allocated_amount` IS NOT NULL GROUP BY `banks`.`code` ORDER BY `avg_allocated_amount` DESC LIMIT 1</code> |
