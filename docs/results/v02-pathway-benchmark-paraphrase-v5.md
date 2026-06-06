# v0.2 Paraphrase Guard v5

Date: 2026-06-05

Retained compact summary. This report intentionally omits the full 137-row case
matrix; regenerate it from the command below when investigating a regression.

## Result

| Policy | Route correct | Route wrong SQL | Route fail-closed | Non-route fail-closed | Unexpected SQL |
|---|---:|---:|---:|---:|---:|
| `current_permissive` | `124/124` | `0` | `0` | `13/13` | `0` |
| `frame_only` | `124/124` | `0` | `0` | `13/13` | `0` |
| `bounded_stage3` | `124/124` | `0` | `0` | `13/13` | `0` |
| `bound_plan` | `124/124` | `0` | `0` | `13/13` | `0` |

## Coverage

- base route cases: `31`;
- paraphrase variants per route: `3`;
- requested route variants generated: `93/93`;
- Stage 3 SQL accepted: `0`;
- frame-promoted route wrong SQL: `0`;
- bound-plan route wrong SQL: `0`.

## Decision

Use this as the product paraphrase regression guard. A future run may replace
this report only if route correctness stays complete and wrong accepted SQL
stays `0`.

## Verification

```bash
uv run python -m semsql_eval pathway-benchmark --schema-variant canonical --paraphrase-variants-per-route 3 --semsql-bin target/debug/semsql.exe --out target/v02/pathway-paraphrase-v5
```

Fresh verification after random-key safety:
`target/v02/pathway-paraphrase-after-random-key-safety-v6`.

Fresh verification after anchor/display fixes:
`target/v02/pathway-paraphrase-after-anchor-display-v8`.

Fresh verification after identityguard fixes:
`target/v02/pathway-paraphrase-after-identityguard-v9`.
