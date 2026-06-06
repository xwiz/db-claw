# Random-Alias Breadth Benchmark v1

Date: 2026-06-05

Retained evidence report. Current status and regression rollups live in
[v02-current-status.md](v02-current-status.md) and
[v02-evidence-ledger.md](v02-evidence-ledger.md).

Purpose: pressure the SemanticAtlas route against random physical table/field
names while relying on labels, vocabulary, relationships, and samples.

## Signal

| Suite | Seeds | Route correct | Wrong SQL | Route fail-closed | Non-route fail-closed |
|---|---|---:|---:|---:|---:|
| business | `1-8,42` | `180/180` | `0` | `0` | `54/54` |
| platform | `1-5,42` | `66/66` | `0` | `0` | `42/42` |
| total | `15 runs` | `246/246` | `0` | `0` | `96/96` |

## Root Cause Fixed

Seeds `1` and `3` initially failed closed on `ba010`
(`support_renewal_intersection`). The router joined customers to tickets but
attached `before April 2024` to a ticket date instead of the customer renewal
date. The validator correctly rejected the partial plan as
`missing_requested_projection`.

The generic fix made date-window subject scoring use the vocabulary-aware
subject resolver. This lets `customers` anchor the date predicate to the
customer/account entity even when the physical table is named like
`t_zephyr_03`.

## Verification

Replay directories:

- `target/v02/pathway-business-random-alias-seed1-breadth-v2`
- `target/v02/pathway-business-random-alias-seed2-breadth-v2`
- `target/v02/pathway-business-random-alias-seed3-breadth-v2`
- `target/v02/pathway-business-random-alias-seed4-breadth-v2`
- `target/v02/pathway-business-random-alias-seed5-breadth-v2`
- `target/v02/pathway-business-random-alias-seed6-breadth-v1`
- `target/v02/pathway-business-random-alias-seed7-breadth-v1`
- `target/v02/pathway-business-random-alias-seed8-breadth-v1`
- `target/v02/pathway-business-random-alias-seed42-vocabdate-v17`
- `target/v02/pathway-platform-random-alias-seed1-breadth-v1`
- `target/v02/pathway-platform-random-alias-seed2-breadth-v1`
- `target/v02/pathway-platform-random-alias-seed3-breadth-v1`
- `target/v02/pathway-platform-random-alias-seed4-breadth-v1`
- `target/v02/pathway-platform-random-alias-seed5-breadth-v1`
- `target/v02/pathway-platform-random-alias-seed42-vocabdate-v1`

Anchors after the fix: canonical `31/31`, paraphrase `124/124`, platform
semantic aliases `11/11`, BI semantic aliases `20/20`, and `cargo test -p
semsql-runtime` `244` tests, all with `0` wrong accepted SQL.

## Next Risk

This proves seeded physical-name robustness inside the generated suites. The
next production signal must come from more real Laravel/Rails/Django/Next/Vue
schemas plus typed fallback packets for rejected routes.
