# Real DB MySQL/MariaDB Typed Fallback Probe Suite

- status: `PASS`
- seeds: `20260604, 20260605, 20260606`
- databases: `fraud_radar`
- provider: `openai`
- families: `rate, grouped_avg, filtered_grouped_avg, value_filtered_grouped_avg, joined_filtered_grouped_avg, multi_joined_filtered_grouped_avg`
- safety mode: `schema-only extraction; bounded non-redacted sample values included; provider may propose typed plans only; SQL is locally rendered/validated and executed read-only with row values discarded`

## Summary

- runs passed: `3/3`
- runs skipped: `0`
- runs failed/error: `0`
- questions: `54`
- selected SQL: `54/54`
- typed fallback selected: `11/54`
- local selected: `43`
- provider calls: `11`
- provider errors: `0`
- render errors: `0`
- execution ok: `54/54`
- expected table/field matches: `54/54`
- expected kinds: `{'conditional_rate': 9, 'grouped_avg': 9, 'filtered_grouped_avg': 9, 'value_filtered_grouped_avg': 9, 'joined_filtered_grouped_avg': 9, 'multi_joined_filtered_grouped_avg': 9}`
- rows retained cases: `0`
- sample-value rows: `441`

## Runs

| Seed | Database | Status | Questions | Selected | Exec OK | Expected | Provider Calls | Rows Retained | Artifact |
|---:|---|---|---:|---:|---:|---:|---:|---:|---|
| 20260604 | `fraud_radar` | `PASS` | `18` | `18` | `18` | `18` | `4` | `0` | `target\realdb_typed_fallback_mariadb_suite_openai_value_v5\seed-20260604` |
| 20260605 | `fraud_radar` | `PASS` | `18` | `18` | `18` | `18` | `4` | `0` | `target\realdb_typed_fallback_mariadb_suite_openai_value_v5\seed-20260605` |
| 20260606 | `fraud_radar` | `PASS` | `18` | `18` | `18` | `18` | `3` | `0` | `target\realdb_typed_fallback_mariadb_suite_openai_value_v5\seed-20260606` |
