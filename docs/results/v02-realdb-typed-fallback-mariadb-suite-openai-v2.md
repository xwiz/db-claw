# Real DB MySQL/MariaDB Typed Fallback Probe Suite

- status: `PASS`
- seeds: `20260604, 20260605, 20260606`
- databases: `fraud_radar, mailer_web`
- provider: `openai`
- families: `grouped_avg, filtered_grouped_avg, joined_filtered_grouped_avg, multi_joined_filtered_grouped_avg`
- safety mode: `schema-only extraction; no sample values; provider may propose typed plans only; SQL is locally rendered/validated and executed read-only with row values discarded`

## Summary

- runs passed: `3/3`
- runs skipped: `0`
- runs failed/error: `0`
- questions: `36`
- selected SQL: `36/36`
- typed fallback selected: `1/36`
- local selected: `35`
- provider calls: `1`
- provider errors: `0`
- render errors: `0`
- execution ok: `36/36`
- expected table/field matches: `36/36`
- expected kinds: `{'grouped_avg': 9, 'filtered_grouped_avg': 9, 'joined_filtered_grouped_avg': 9, 'multi_joined_filtered_grouped_avg': 9}`
- rows retained cases: `0`
- sample-value rows: `0`

## Runs

| Seed | Database | Status | Questions | Selected | Exec OK | Expected | Provider Calls | Rows Retained | Artifact |
|---:|---|---|---:|---:|---:|---:|---:|---:|---|
| 20260604 | `mailer_web` | `PASS` | `12` | `12` | `12` | `12` | `1` | `0` | `target\realdb_typed_fallback_mariadb_suite_openai_v2\seed-20260604` |
| 20260605 | `fraud_radar` | `PASS` | `12` | `12` | `12` | `12` | `0` | `0` | `target\realdb_typed_fallback_mariadb_suite_openai_v2\seed-20260605` |
| 20260606 | `fraud_radar` | `PASS` | `12` | `12` | `12` | `12` | `0` | `0` | `target\realdb_typed_fallback_mariadb_suite_openai_v2\seed-20260606` |
