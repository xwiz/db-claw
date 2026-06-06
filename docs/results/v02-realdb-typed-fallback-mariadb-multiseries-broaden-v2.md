# v0.2 MariaDB Multi-Series Typed Fallback Broaden v2
Date: 2026-06-05

Purpose: prove native local recovery for schema-derived `metric by segment over
time` BI shapes across multiple real MariaDB schemas, without provider SQL or
static query maps.

## Result

Pass. Five real schemas generated and executed `15/15` multi-series grouped
average probes. Every accepted query matched the expected metric, segment, and
time fields, shaped as `multi_series_chart`, executed read-only, and retained no
row values.

| Database | Questions | Selected | Executed | Expected Match | Shape OK | Rows Retained | Replay |
|---|---:|---:|---:|---:|---:|---:|---|
| `el_biblio` | 3 | 3 | 3 | 3 | 3 | 0 | `target/v02/realdb-typed-fallback-mariadb-multiseries-broaden-v2/el_biblio` |
| `fraudv_go` | 3 | 3 | 3 | 3 | 3 | 0 | `target/v02/realdb-typed-fallback-mariadb-multiseries-broaden-v2/fraudv_go` |
| `guardrail` | 3 | 3 | 3 | 3 | 3 | 0 | `target/v02/realdb-typed-fallback-mariadb-multiseries-broaden-v2/guardrail` |
| `hostshell` | 3 | 3 | 3 | 3 | 3 | 0 | `target/v02/realdb-typed-fallback-mariadb-multiseries-broaden-v2/hostshell` |
| `fraud_radar` | 3 | 3 | 3 | 3 | 3 | 0 | `target/v02/realdb-typed-fallback-mariadb-multiseries-broaden-v2/fraud_radar` |

Total: `15/15` selected, executed, expected-field matched, and result-shape
matched; provider calls `0`; rows retained `0`.

## Regression Fixed

The v1 broaden run failed closed on exact multi-word measure phrases such as
`vote_spiritual_sum`, `vote_effort_sum`, and `target_value`. The runtime SQL was
already correct, but the bound-plan validator rejected it as
`grouped_measure_entity_mismatch`. The fix treats exact multi-token selected
measure phrases as strong measure evidence while preserving the existing anchor
mismatch guard for weak one-token measures.

## Verification

```bash
cargo test -p semsql-runtime graph_schema_atlas_tests -- --nocapture
cargo build -p semsql-cli
uv run python -m semsql_eval realdb-typed-fallback-mysql --family multi_series_grouped_avg --provider none --strict ...
```

Runtime atlas: `119/119`. Five local MariaDB runs: all pass.
