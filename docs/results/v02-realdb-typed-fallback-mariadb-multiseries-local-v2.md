# Real DB MySQL/MariaDB Typed Fallback Probe

- status: `PASS`
- database: `fraud_radar`
- graph: `target\v02\realdb-typed-fallback-mariadb-multiseries-local-v2\graphs\fraud_radar.schemaonly.semsql`
- provider: `none`
- families: `multi_series_grouped_avg`
- high-risk schema: `False`
- safety mode: `schema-only extraction; no sample values; provider may propose typed plans only; SQL is locally rendered/validated and executed read-only with row values discarded`

## Summary

- questions: `2`
- selected SQL: `2/2`
- typed fallback selected: `0/2`
- local selected: `2`
- provider calls: `0`
- provider errors: `0`
- provider readiness: `2/2 configured, 0 unconfigured; providers={'none': 2}, missing_env={}`
- render errors: `0`
- execution ok: `2/2`
- expected table/field matches: `2/2`
- expected kinds: `{'multi_series_grouped_avg': 2}`
- result shape ok: `2/2`
- result shapes: `{'multi_series_chart': 2}`
- rows retained cases: `0`
- sample-value rows: `0`
- packet schema evidence ok: `True`
- full rejected packet schema evidence: `0 checked, 0 missing records, 0 missing facts`
- compact provider request schema evidence: `0 checked, 0 missing records, 0 missing facts`

## Records

| # | Question | Expected | Source | Provider Calls | Render | Exec | Rows Retained | Expected Match | Shape | OK | SQL |
|---:|---|---|---|---:|---|---|---|---|---|---|---|
| 1 | `show average amount by status over updated at for fraud reports` | `fraud_reports.amount by fraud_reports.status over fraud_reports.updated_at` | `local` | `0` | `None` | `ok` | `False` | `True` | `multi_series_chart` | `True` | <code>SELECT `fraud_reports`.`updated_at`, `fraud_reports`.`status`, AVG(`fraud_reports`.`amount`) AS `avg_amount` FROM `fraud_reports` WHERE `fraud_reports`.`amount` IS NOT NULL GROUP BY `fraud_reports`.`updated_at`, `fraud_reports`.`status` ORDER BY `avg_amount` DESC</code> |
| 2 | `show average amount by status over assigned at for fraud reports` | `fraud_reports.amount by fraud_reports.status over fraud_reports.assigned_at` | `local` | `0` | `None` | `ok` | `False` | `True` | `multi_series_chart` | `True` | <code>SELECT `fraud_reports`.`assigned_at`, `fraud_reports`.`status`, AVG(`fraud_reports`.`amount`) AS `avg_amount` FROM `fraud_reports` WHERE `fraud_reports`.`amount` IS NOT NULL GROUP BY `fraud_reports`.`assigned_at`, `fraud_reports`.`status` ORDER BY `avg_amount` DESC</code> |
