# MariaDB Typed Fallback Refresh v17

Date: 2026-06-05. Superseded by
[refresh v20](v02-realdb-typed-fallback-mariadb-suite-refresh-v20.md).

Purpose: refresh the local MariaDB real-schema suite after doc cleanup and
runtime changes, using read-only execution and no provider calls.

## Result

- Status: fail-closed regression, not wrong accepted SQL.
- Scope: seeds `20260604`, `20260605`, `20260606`; DBs `fraud_radar`,
  `mailer_web`; families `rate`, `grouped_avg`, `filtered_grouped_avg`,
  `value_filtered_grouped_avg`, `joined_filtered_grouped_avg`,
  `multi_joined_filtered_grouped_avg`.
- Outcome: `45/53` selected and executed; `8/53` unresolved; `0` provider
  calls; packet schema evidence present.

## Buckets

| Bucket | Count | Signal |
|---|---:|---|
| boolean predicate binding | 5 | `have police report`, `have auto outreach enabled` do not reliably promote |
| large-schema timeout | 2 | mailer grouped metrics time out before a typed plan |
| promotion gap | 1 | runtime routes grouped aggregate, final packet still returns `needs_model` |

## Anchors

- Artifacts: `target/v02/realdb-typed-fallback-mariadb-suite-refresh-v17`
- Key graph: `target/v02/realdb-typed-fallback-mariadb-suite-refresh-v17/seed-20260606/graphs/fraud_radar.schemaonly.semsql`
- Confirmed field: `fraud_reports.has_police_report` exists as boolean with
  sampled values.

## Next Fix

Fixed by generic boolean-predicate competition, large-schema candidate
narrowing, and grouped-measure validator tightening. Use v20 as current.
