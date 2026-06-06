# v0.2 Semantic-Alias Boolean Sample Domain v12

Date: 2026-06-05

Retained benchmark summary for generic boolean-domain inference in grouped
conditional rates. Current gate numbers live in `v02-evidence-ledger.md`.

## Change

Fields with boolean-like sample domains (`0/1`, `true/false`, `yes/no`) can now
use field vocabulary aliases as boolean conditions. This lets semantic aliases
such as "SLA breach" map to a physical integer field like `sla_missed` without a
query-specific route.

## Result

| Suite | v12 | Wrong accepted SQL |
|---|---:|---:|
| Platform semantic aliases | `11/11` | `0` |
| BI semantic aliases | `20/20` | `0` |

Recovered since v11:

- `ba020`, `ratio_by_joined_dimension`: `SLA breach rate by segment`.

## Artifacts

- `target/v02/pathway-platform-semantic-alias-boolean-sample-domain-v12/pathway_benchmark.md`
- `target/v02/pathway-platform-semantic-alias-boolean-sample-domain-v12/pathway_benchmark.json`
- `target/v02/pathway-business-semantic-alias-boolean-sample-domain-v12/pathway_benchmark.md`
- `target/v02/pathway-business-semantic-alias-boolean-sample-domain-v12/pathway_benchmark.json`

Fresh post-random-key-safety verification:

- `target/v02/pathway-platform-semantic-alias-after-random-key-safety-v14/`
- `target/v02/pathway-business-semantic-alias-after-random-key-safety-v14/`

Fresh post-anchor/display verification:

- `target/v02/pathway-platform-semantic-alias-after-anchor-display-v16/`
- `target/v02/pathway-business-semantic-alias-after-anchor-display-v16/`

Fresh post-identityguard verification:

- `target/v02/pathway-platform-semantic-alias-after-identityguard-v17/`
- `target/v02/pathway-business-semantic-alias-after-identityguard-v17/`

## Verification

- `cargo fmt --check`
- `cargo test -p semsql-runtime graph_schema_atlas_tests -- --nocapture`
- `cargo build -p semsql-cli`
- `uv run python -m semsql_eval pathway-benchmark --suite platform --schema-variant semantic_alias --semsql-bin target/debug/semsql.exe --out target/v02/pathway-platform-semantic-alias-boolean-sample-domain-v12`
- `uv run python -m semsql_eval pathway-benchmark --suite business --schema-variant semantic_alias --semsql-bin target/debug/semsql.exe --out target/v02/pathway-business-semantic-alias-boolean-sample-domain-v12`
