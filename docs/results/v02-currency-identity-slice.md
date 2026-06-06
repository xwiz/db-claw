# v0.2 Currency/Identity List Slice

Date: 2026-06-02

## Summary

This slice fixes a BI/commerce-shaped lookup without adding a database-specific
router.

The route is generic:

- sampled ISO currency values can be grounded through field-compatible natural
  aliases such as `euro` -> `EUR`, but only when the field looks like a currency
  field;
- entity-list queries that ask for a `description` field can carry the entity
  identity key beside the description when the entity itself is mentioned;
- those identity-description list projections use `SELECT DISTINCT` when routed
  through fact joins, so the answer is a unique entity list rather than repeated
  transaction rows;
- final SQL promotion accepts only this narrow identity-plus-description shape.

The regression tests use neutral `items`, `orders`, and `accounts` tables rather
than BIRD table names.

## Results

Fresh artifacts from the cleaned runtime:

- QueryFrame canary suite:
  `docs/results/v02-queryframe-canary-suite-currency-identity-v1.md`
  - `PASS`
  - routed exec accuracy: `144/144`
  - rejects fail-closed: `18/18`
- Focused BIRD trace:
  `artifacts/results-json/docs-results/v02-bird-failure-trace-currency-identity-v1-report.json`
  - `11/13` correct
  - `2` wrong
  - `0` bails
  - `0` errors
  - `0` timeouts
  - stage breakdown: `stage_0a=11`, `stage_3=2`

Recovered focused BIRD example:

- `idx 1503`: product descriptions for products bought in euro now routes
  through QueryFrame, grounds `euro` to sampled `customers.currency = 'EUR'`,
  joins through the transaction fact table, and projects distinct product
  identity plus description.

## Remaining Buckets

- `idx 214` and `idx 245` were later recovered by
  `v02-focused13-full-recovery-slice.md`.

## Interpretation

Focused cleaned BIRD recovery is now:

- schema-description slice: `4/13`
- MetricAtlas slice: `7/13`
- rank-window slice: `8/13`
- attribute-superlative slice: `9/13`
- person/event lookup slice: `10/13`
- currency/identity list slice: `11/13`
- focused 13 full-recovery slice: `13/13`

The next deterministic work should be driven by a fresh first-100 or
stratified-100 diagnostic, not by the now-closed focused failure set.
