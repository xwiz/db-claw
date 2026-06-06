# v0.2 Cleaned Runtime Rerun

Date: 2026-06-02

## Summary

This rerun used fresh debug and release `semsql` binaries built after the
static-shortcut cleanup and the generic grouped-ranking fix.

Product canaries are green on the cleaned runtime. BIRD is not.

## Product Canaries

| check | result | artifact |
|---|---:|---|
| SQLite QueryFrame suite | `9/9` runs, `144/144` routed, `18/18` rejected | `v02-queryframe-canary-suite-cleaned-v3.*` |
| MySQL/MariaDB QueryFrame canary | `16/16` routed, `2/2` rejected | `v02-queryframe-canary-mysql-cleaned-v2.*` |
| high-risk MariaDB schema-only probe | `10/10` count-only, `2/2` unsafe prompts not executed, `0` sample rows | `v02-realdb-random-cloudspace-kyc-schemaonly-cleaned-v2.*` |
| seeded MariaDB schema-only suite | `3/3` database draws, `21/21` count-only, `0` sample rows | `v02-realdb-random-mariadb-schemaonly-suite-cleaned-v2.*` |
| `mailer_web` shard audit | `REVIEW`, metadata/source-only, no row data sampled | `v02-realdb-mailer-web-sharding-audit-cleaned-v2.*` |

The first cleaned SQLite rerun failed at `126/144` because top-N dimension
ranking by a related amount was rendered as row ranking instead of grouped
aggregate ranking. The runtime now renders that generic frame as:

```sql
SELECT dimension, SUM(measure) AS total_measure
...
GROUP BY dimension
ORDER BY total_measure DESC
LIMIT n
```

That is schema-derived behavior, not a product/benchmark shortcut.

## Benchmark Smoke

| check | result | read |
|---|---:|---|
| BIRD first 20 | `0/20`, `1` structural error | do not launch full dev from this state |
| BIRD stratified 50 | `3/50 = 6.00%`, `1` timeout bucket | broad benchmark gap after shortcut removal |

Full BIRD dev was not launched because the smoke results already fail the
benchmark stoplight. A multi-hour full run would be low-signal until the
benchmark path is rebuilt around reusable schema/value frames rather than the
removed database-family shortcuts.

## Runtime Fixes From The Rerun

- Added generic grouped aggregate ranking for dimension-by-related-measure top-k
  questions.
- Added fail-closed handling for unsupported `rate` analytics unless a real
  metric frame/catalog handles them.
- Kept rate/ratio/percentage queries out of local generic SQL when the metric is
  not explicitly modeled.

## Verification

```text
cargo build -p semsql-cli --features semsql-cli/onnx
cargo build -p semsql-cli --release --features semsql-cli/onnx
cargo check -p semsql-runtime --features onnx --lib
cargo clippy -p semsql-runtime --features onnx --lib --tests -- -D warnings
cargo test -p semsql-runtime --features onnx --lib --tests --no-fail-fast
```

All commands above passed on the cleaned runtime.

## Next

Do not chase BIRD by reintroducing shortcuts. The next benchmark work should be
a reusable schema/value-frame track: metric catalogs, explicit ratio/rate
frames, richer projection/owner frames, and typed LLM proposal packets for
queries the local compiler rejects.
