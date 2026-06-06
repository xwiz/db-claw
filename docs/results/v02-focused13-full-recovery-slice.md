# v0.2 Focused 13 Full-Recovery Slice

Date: 2026-06-02

## Summary

This slice closes the remaining focused BIRD failure buckets without restoring
static benchmark routers.

New generic behavior:

- field-compatible chemical element aliases, such as `tin` -> `sn` and
  `iodine` -> `i`, only activate on fields that look like element/code fields;
- string predicates can use `!=` when the matched value mention appears in a
  negated context;
- `what type/kind of` lookup prompts project distinct display values;
- `average number of related things per filtered entity` prompts route through
  an FK bridge as `COUNT(bridge.related_key) / COUNT(subject.key)`.

The regression tests use neutral specimens and order/item bridge schemas, not
the BIRD table names.

## Results

Fresh artifacts from the cleaned runtime:

- QueryFrame canary suite:
  `docs/results/v02-queryframe-canary-suite-focused13-full-recovery-v1.md`
  - `PASS`
  - routed exec accuracy: `144/144`
  - rejects fail-closed: `18/18`
- Focused BIRD trace:
  `artifacts/results-json/docs-results/v02-bird-failure-trace-focused13-full-recovery-v1-report.json`
  - `13/13` correct
  - `0` wrong
  - `0` bails
  - `0` errors
  - `0` timeouts
  - stage breakdown: `stage_0a=13`

Recovered focused BIRD examples:

- `idx 214`: grounded `tin` to sampled `atom.element = 'sn'`, preserved the
  negated predicate, and projected distinct molecule labels.
- `idx 245`: grounded `iodine` to sampled `atom.element = 'i'` and rendered the
  bridge-count average over `connected`.

## Interpretation

Focused cleaned BIRD recovery is now:

- schema-description slice: `4/13`
- MetricAtlas slice: `7/13`
- rank-window slice: `8/13`
- attribute-superlative slice: `9/13`
- person/event lookup slice: `10/13`
- currency/identity list slice: `11/13`
- focused 13 full-recovery slice: `13/13`

This is a strong regression stoplight for the known focused failure set. It is
not a full BIRD-dev claim. The next benchmark step should be a fresh stratified
or first-100 run to discover the next unseen failure buckets before attempting
the full dev gate.
