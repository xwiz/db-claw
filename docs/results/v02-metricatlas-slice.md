# v0.2 MetricAtlas Slice

Date: 2026-06-02. Historical slice; current numbers live in
[v02-evidence-ledger.md](v02-evidence-ledger.md). The `idx 736` bucket noted
here was later recovered in `v02-attribute-superlative-slice.md`.

## Purpose

Added generic state-machine support for metric/ranking lookup questions without
BIRD-specific shortcuts. This slice proved that metric phrases can be handled by
schema-derived numerator/denominator, order-field, threshold, and projection
evidence.

## Preserved Contract

- Rate fields are derived from compatible numerator and denominator candidates.
- Answer fields such as phone/name/address stay projections.
- Numeric/acronym field names such as `NumGE1500` do not become duplicate value
  predicates.
- Metric/score/stat tables may be row-level lookup sources; event/fact tables
  may still group and sum.
- Regression coverage includes K-12 rate selection, SAT excellence ordering,
  compact coded fields, and code-like identity lookup `00D4`.

## Result

| Signal | Value |
|---|---:|
| QueryFrame canary | `PASS`, routed `144/144`, rejects `18/18` |
| Focused BIRD trace | `7/13` correct, `4` wrong, `2` bails |
| Errors/timeouts | `0/0` |
| Stage breakdown | `stage_0a=7`, `stage_3=4`, `needs_model=1` |

Recovered examples: `idx 0`, `idx 7`, `idx 13`.

## Remaining Buckets At This Slice

`idx 17` rank/window was later recovered; `idx 214`, `245`, `859`, and `1503`
remained model/state-machine gaps. Do not use this slice as broad BIRD
readiness evidence.
