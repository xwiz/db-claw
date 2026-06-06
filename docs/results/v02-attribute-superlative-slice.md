# v0.2 Attribute Superlative Slice

Date: 2026-06-02

## Summary

This slice adds a conservative state-machine route for qualitative
attribute-measure superlatives such as `least intelligent person`.

The route is generic:

- the question must contain a known qualitative superlative trigger;
- the graph must contain an attribute/metric catalog field with a sampled value
  matching the requested concept, such as `Intelligence`;
- the graph must contain a numeric bridge value field related to both the
  subject entity and the attribute/metric catalog entity;
- joins come only from FK relationships;
- final SQL promotion is gated by grounded predicates, numeric order field,
  bounded joins, and bounded limit.

This replaced an old entity-shaped scoring nudge with a graph-proven frame. The
new regression test uses neutral `people`, `person_attributes`, and
`attributes` tables, not a BIRD table name.

## Results

Fresh artifacts from the current runtime:

- QueryFrame canary suite:
  `docs/results/v02-queryframe-canary-suite-attribute-v1.md`
  - `PASS`
  - routed exec accuracy: `144/144`
  - rejects fail-closed: `18/18`
- Focused BIRD trace:
  `artifacts/results-json/docs-results/v02-bird-failure-trace-attribute-v2-report.json`
  - `9/13` correct
  - `3` wrong
  - `1` bail
  - `0` errors
  - `0` timeouts
  - stage breakdown: `stage_0a=9`, `stage_3=3`

Recovered focused BIRD example:

- `idx 736`: `dumbest superhero` now routes through
  `runtime_graph_query_frame_attribute`, resolves the grounded attribute value
  `Intelligence`, orders `hero_attribute.attribute_value ASC`, and projects
  `superhero.superhero_name`.

No previous `rank-v1` correct examples regressed in the focused trace.

## Remaining Buckets

- `idx 214`: toxicology negation plus element-code mapping
  (`tin` -> `sn`, `!=` predicate).
- `idx 245`: average edge count ratio over atom/bond graph relationships.
- `idx 859`: Formula 1 person-name plus race-number lookup was later recovered
  by `v02-person-event-slice.md`.
- `idx 1503`: currency/product transaction lookup still falls through to a bad
  Stage 3 value path.

## Interpretation

Focused cleaned BIRD recovery is now:

- schema-description slice: `4/13`
- MetricAtlas slice: `7/13`
- rank-window slice: `8/13`
- attribute-superlative slice: `9/13`
- person/event lookup slice: `10/13`
- currency/identity list slice: `11/13`

The next useful deterministic work is not broader Stage 3 tuning. It is adding
small, evidence-gated frames for:

- chemical/code synonym predicates and negation;
- count-per-entity graph-edge ratios;
