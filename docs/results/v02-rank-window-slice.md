# v0.2 Rank Window Slice

Date: 2026-06-02. Historical slice; `idx 736` was later recovered in
`v02-attribute-superlative-slice.md`. Keep this only as the rank-window record;
current release numbers live in `v02-evidence-ledger.md`.

## Purpose

Added a conservative state-machine route for explicit rank/window questions. It
requires explicit rank intent, a numeric rank key from schema/vocabulary
evidence, bounded predicates, requested projection fields, FK-backed joins, and
`RANK() OVER (ORDER BY ...)`.

## Preserved Contract

- Rank routing runs before broad complex-shape rejection.
- Projection focus parses `showing/show/display/list` tails.
- Distinctive projection terms such as `charter` beat generic terms such as
  `number`.
- Threshold predicates attach to the selected rank measure, not sibling score
  fields.
- Final SQL promotion requires numeric rank field, grounded predicates, bounded
  joins, and explicit rank intent.

## Result

| Signal | Value |
|---|---:|
| QueryFrame canary | `PASS`, routed `144/144`, rejects `18/18` |
| Focused BIRD trace | `8/13` correct, `4` wrong, `1` bail |
| Errors/timeouts | `0/0` |
| Stage breakdown | `stage_0a=8`, `stage_3=4` |

Recovered `idx 17`: charter numbers ranked by writing score with
`RANK() OVER (ORDER BY AvgScrWrite DESC)`.

Remaining at this slice: `idx 214`, `245`, `736`, `859`, `1503`. The next
useful fixes were semantic attribute mapping and typed rejected-query packets,
not more simple metric/rank frames.
