# Real DB MariaDB Schema-Only Probe

Date: 2026-06-04

Compact retained report. Do not expand this file with row-level SQL matrices;
replay artifacts belong under `target/`.

## Result

- status: `PASS`
- database: `cloudspace_kyc_dashboard`
- graph: `target/realdb_analytics_probe_cloudspace_v3/graphs/cloudspace_kyc_dashboard.schemaonly.semsql`
- safety mode: schema-only extraction, no sample values retained, governed
  count/analytics execution only

| Signal | Value |
|---|---:|
| questions | `21` |
| required contract | `12/12` |
| routed | `19` |
| count-only executed | `10/10` |
| governed analytics executed | `9/9` |
| safe not-executed routes/rejects | `2` |
| execution errors | `0` |
| sample-value rows retained | `0` |
| semantic ok or safe not-executed | `21/21` |

## Coverage

Covered real-schema table counts, date counts, grouped counts, and numeric
averages on a high-risk MariaDB schema. Two list-style password/PIN prompts
were correctly not executed.

## Decision Impact

This supports the production safety claim for schema-only real DB probing. It
does not prove broad BI planning; those gates live in
`v02-evidence-ledger.md`.
