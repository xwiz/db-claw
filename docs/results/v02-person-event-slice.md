# v0.2 Person/Event Lookup Slice

Date: 2026-06-02

## Summary

This slice adds a conservative state-machine route for person plus event-number
lookups such as `What is Ada Lovelace's Q1 result in event No. 354?`.

The route is generic:

- the person name must be grounded by sampled first-name and last-name fields on
  the same entity;
- the requested result/projection field must be visible in the graph;
- the event/order/race identifier must be explicitly marked by words such as
  `No.`, `number`, or `id`;
- joins come only from FK relationships;
- final SQL promotion is gated by grounded person predicates, the marked numeric
  event predicate, a bounded join path, and a single safe projection.

This slice also fixed a graph-build reliability bug: zero-byte graph-cache files
are now treated as invalid and rebuilt, and DB description CSVs are ingested
with UTF-8 replacement so one bad byte cannot silently leave an eval graph empty.

## Results

Fresh artifacts from the cleaned runtime:

- QueryFrame canary suite:
  `docs/results/v02-queryframe-canary-suite-person-event-clean-v1.md`
  - `PASS`
  - routed exec accuracy: `144/144`
  - rejects fail-closed: `18/18`
- Focused BIRD trace:
  `artifacts/results-json/docs-results/v02-bird-failure-trace-person-event-clean-v1-report.json`
  - `10/13` correct
  - `3` wrong
  - `0` bails
  - `0` errors
  - `0` timeouts
  - stage breakdown: `stage_0a=10`, `stage_3=3`

Recovered focused BIRD example:

- `idx 859`: Formula 1 `Bruno Senna` plus `race No. 354` now routes through
  `runtime_graph_query_frame_person_event`, grounds the sampled person name,
  binds the marked numeric event id, joins through FK metadata, and projects the
  requested `q1` field.

No previous focused correct examples regressed after removing benchmark-shaped
test names and a tiny entity-name preference from the runtime.

## Remaining Buckets

- `idx 214`: toxicology negation plus element-code mapping
  (`tin` -> `sn`, `!=` predicate).
- `idx 245`: average edge count ratio over atom/bond graph relationships.
- `idx 1503`: currency/product transaction lookup was later recovered by
  `v02-currency-identity-slice.md`.

## Interpretation

Focused cleaned BIRD recovery is now:

- schema-description slice: `4/13`
- MetricAtlas slice: `7/13`
- rank-window slice: `8/13`
- attribute-superlative slice: `9/13`
- person/event lookup slice: `10/13`
- currency/identity list slice: `11/13`

The next useful deterministic work is the toxicology/code-alias bucket, because
the commerce/BI-shaped currency/product lookup is now covered by a generic
field-compatible route.
