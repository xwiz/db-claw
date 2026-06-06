# Real DB MySQL/MariaDB Typed Fallback Probe

- status: `PASS`
- database: `fraud_radar`
- graph: `target\realdb_typed_fallback_mariadb_rates_v1\graphs\fraud_radar.schemaonly.semsql`
- provider: `openai`
- high-risk schema: `False`
- safety mode: `schema-only extraction; no sample values; provider may propose typed plans only; SQL is locally rendered/validated and executed read-only with row values discarded`

## Summary

- questions: `3`
- selected SQL: `3/3`
- typed fallback selected: `3/3`
- local selected: `0`
- provider calls: `3`
- provider errors: `0`
- render errors: `0`
- execution ok: `3/3`
- expected table/field matches: `3/3`
- rows retained cases: `0`
- sample-value rows: `0`

## Records

| # | Question | Expected | Source | Provider Calls | Render | Exec | Rows Retained | Expected Match | OK | SQL |
|---:|---|---|---|---:|---|---|---|---|---|---|
| 1 | `what percentage of fraud reports are closed` | `fraud_reports.is_closed` | `typed_fallback` | `1` | `True` | `ok` | `False` | `True` | `True` | <code>SELECT CAST(SUM(CASE WHEN `fraud_reports`.`is_closed` = 1 THEN 1 ELSE 0 END) AS DOUBLE) * 100.0 / NULLIF(COUNT(`fraud_reports`.`id`), 0) AS `pct_fraud_reports_closed` FROM `fraud_reports`</code> |
| 2 | `what percentage of entities are blacklisted` | `entities.is_blacklisted` | `typed_fallback` | `1` | `True` | `ok` | `False` | `True` | `True` | <code>SELECT CAST(SUM(CASE WHEN `entities`.`is_blacklisted` = 1 THEN 1 ELSE 0 END) AS DOUBLE) * 100.0 / NULLIF(COUNT(`entities`.`id`), 0) AS `pct_entities_blacklisted` FROM `entities`</code> |
| 3 | `what percentage of fraud files are verified` | `fraud_files.is_verified` | `typed_fallback` | `1` | `True` | `ok` | `False` | `True` | `True` | <code>SELECT CAST(SUM(CASE WHEN `fraud_files`.`is_verified` = 1 THEN 1 ELSE 0 END) AS DOUBLE) * 100.0 / NULLIF(COUNT(*), 0) AS `pct_fraud_files_verified` FROM `fraud_files`</code> |
