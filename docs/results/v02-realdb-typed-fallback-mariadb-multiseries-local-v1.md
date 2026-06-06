# Real DB MySQL/MariaDB Typed Fallback Probe

- status: `FAIL`
- database: `fraud_radar`
- graph: `target\v02\realdb-typed-fallback-mariadb-multiseries-local-v1\graphs\fraud_radar.schemaonly.semsql`
- provider: `none`
- families: `multi_series_grouped_avg`
- high-risk schema: `False`
- safety mode: `schema-only extraction; no sample values; provider may propose typed plans only; SQL is locally rendered/validated and executed read-only with row values discarded`

## Summary

- questions: `2`
- selected SQL: `0/2`
- typed fallback selected: `0/2`
- local selected: `0`
- provider calls: `0`
- provider errors: `0`
- provider readiness: `2/2 configured, 0 unconfigured; providers={'none': 2}, missing_env={}`
- render errors: `0`
- execution ok: `0/2`
- expected table/field matches: `0/2`
- expected kinds: `{'multi_series_grouped_avg': 2}`
- result shape ok: `0/2`
- result shapes: `{'missing': 2}`
- rows retained cases: `0`
- sample-value rows: `0`
- packet schema evidence ok: `True`
- full rejected packet schema evidence: `2 checked, 0 missing records, 0 missing facts`
- compact provider request schema evidence: `2 checked, 0 missing records, 0 missing facts`

## Records

| # | Question | Expected | Source | Provider Calls | Render | Exec | Rows Retained | Expected Match | Shape | OK | SQL |
|---:|---|---|---|---:|---|---|---|---|---|---|---|
| 1 | `show average amount by status over updated at for fraud reports` | `fraud_reports.amount by fraud_reports.status over fraud_reports.updated_at` | `None` | `0` | `None` | `skipped` | `False` | `False` | `missing` | `False` | <code></code> |
| 2 | `show average amount by status over assigned at for fraud reports` | `fraud_reports.amount by fraud_reports.status over fraud_reports.assigned_at` | `None` | `0` | `None` | `skipped` | `False` | `False` | `missing` | `False` | <code></code> |
