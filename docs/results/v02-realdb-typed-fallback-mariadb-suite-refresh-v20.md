# MariaDB Typed Fallback Refresh v20

Date: 2026-06-05

Purpose: verify the v17 regression fixes on local MariaDB real schemas using
read-only execution, bounded sample values, and no provider calls.

## Result

- Status: pass.
- Scope: seeds `20260604`, `20260605`, `20260606`; DBs `fraud_radar`,
  `mailer_web`; families `rate`, `grouped_avg`, `filtered_grouped_avg`,
  `value_filtered_grouped_avg`, `joined_filtered_grouped_avg`,
  `multi_joined_filtered_grouped_avg`.
- Outcome: `53/53` selected, `53/53` executed, `53/53` expected table/field
  matches, `0` provider calls, `0` rows retained.
- Artifacts: `target/v02/realdb-typed-fallback-mariadb-suite-refresh-v20`

## Fixes Proven

- Multi-token boolean phrases outrank component sample values.
- `enabled` does not become `active` when consumed by a longer boolean phrase.
- Entity modifiers such as `external mail accounts` do not leak into unrelated
  boolean filters.
- Large-schema grouped metrics narrow fields, samples, and vocabulary before
  predicate extraction.
- Grouped-measure validation accepts strongly anchored selected measures despite
  full-schema alias collisions.
