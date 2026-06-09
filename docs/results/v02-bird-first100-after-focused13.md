# v0.2 BIRD First-100 After Focused Recovery

Date: 2026-06-02

## Summary

After closing the focused 13-example failure set, a fresh BIRD first-100 run is
still weak:

- retained source: this summary; the bulky raw JSON was removed during artifact
  cleanup
- total: `100`
- correct: `5`
- wrong: `81`
- bails: `12`
- errors: `2`
- timeouts: `3`
- exec accuracy: `5.00%`
- stage breakdown: `stage_0a=47`, `stage_3=36`, `needs_model=12`,
  `stage2_structural_error=1`, `stage4_render_error=1`, `timeout=3`

The focused recovery was real, but it was a regression stoplight for known
failure buckets, not broad benchmark completion.

## What The Run Shows

The next failures are mostly not the same as the focused 13:

- California-schools dominates the first 89 examples and needs richer semantic
  vocabulary for code fields and institutional phrases: charter/direct funding,
  virtual/magnet flags, grade spans, SOC/DOC/EdOps codes, NCES identifiers, and
  provision-status labels.
- Metric/ranking frames need more compositional support: top-by-one-measure then
  project another field, nth-ranked ranges, grouped top-k, most-common values,
  date/year ranges, and metric filters followed by a different metric
  projection.
- Predicate parsing is still too permissive in broad runs: grades, years,
  county/city names, administrator names, and code-like identifiers are often
  treated as unrelated values or fields.
- Stage 3 fallback remains hazardous: many wrong answers are elaborate
  slot-filled SQL after QueryFrame rejects or cannot prove a route.
- Infrastructure buckets remain: `3` query timeouts, `1` Stage 2 structural
  error, and `1` Stage 4 render error.

## Correct Examples

The five correct examples are all California-schools questions already covered
by the deterministic frames:

- `idx 0`: eligible-free-rate top lookup.
- `idx 7`: SAT test-taker top lookup with phone projection.
- `idx 13`: top-3 SAT excellence rate phone lookup.
- `idx 17`: rank-window writing-score lookup.
- `idx 62`: non-chartered Los Angeles count with eligible-meal rate threshold.

## Next Direction

The next useful milestone is not another focused hand-picked patch. It should be
a California-schools semantic vocabulary and compositional-frame pass:

- ingest/derive code-value dictionaries from schema descriptions and sampled DB
  values for boolean/code fields;
- add date/year range normalization and ordinal/rank-offset support;
- add top-by-measure then project-related-field frames;
- add grouped top-k / most-common frames;
- fail closed before Stage 3 when broad frame evidence is incomplete.

The next validation run should be first-100 again, then stratified-100 once the
California-schools cluster stops dominating the failure count.
