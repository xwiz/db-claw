# Real DB MySQL/MariaDB Typed Fallback Probe Suite

- status: `FAIL`
- seeds: `20260604, 20260605, 20260606`
- databases: `fraud_radar, mailer_web`
- provider: `none`
- families: `rate, grouped_avg, filtered_grouped_avg, value_filtered_grouped_avg, joined_filtered_grouped_avg, multi_joined_filtered_grouped_avg`
- safety mode: `schema-only extraction; bounded non-redacted sample values included; provider may propose typed plans only; SQL is locally rendered/validated and executed read-only with row values discarded`

## Summary

- runs passed: `1/3`
- runs skipped: `0`
- runs failed/error: `2`
- questions: `53`
- selected SQL: `46/53`
- typed fallback selected: `0/53`
- local selected: `46`
- provider calls: `0`
- provider errors: `0`
- provider readiness: `53/53 configured, 0 unconfigured; providers={'none': 53}, missing_env={}`
- render errors: `0`
- execution ok: `46/53`
- expected table/field matches: `46/53`
- expected kinds: `{'conditional_rate': 9, 'grouped_avg': 9, 'filtered_grouped_avg': 9, 'value_filtered_grouped_avg': 8, 'joined_filtered_grouped_avg': 9, 'multi_joined_filtered_grouped_avg': 9}`
- rows retained cases: `0`
- sample-value rows: `627`
- packet schema evidence ok: `True`
- full rejected packet schema evidence: `7 checked, 0 missing records, 0 missing facts`
- compact provider request schema evidence: `7 checked, 0 missing records, 0 missing facts`

## Runs

| Seed | Database | Status | Questions | Selected | Exec OK | Expected | Provider Calls | Rows Retained | Artifact |
|---:|---|---|---:|---:|---:|---:|---:|---:|---|
| 20260604 | `mailer_web` | `FAIL` | `17` | `11` | `11` | `11` | `0` | `0` | `target\realdb_typed_fallback_mariadb_suite_local_value_multidb_v14\seed-20260604` |
| 20260605 | `fraud_radar` | `FAIL` | `18` | `17` | `17` | `17` | `0` | `0` | `target\realdb_typed_fallback_mariadb_suite_local_value_multidb_v14\seed-20260605` |
| 20260606 | `fraud_radar` | `PASS` | `18` | `18` | `18` | `18` | `0` | `0` | `target\realdb_typed_fallback_mariadb_suite_local_value_multidb_v14\seed-20260606` |
