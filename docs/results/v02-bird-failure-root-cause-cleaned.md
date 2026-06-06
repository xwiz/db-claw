# v0.2 Cleaned BIRD Root Cause

Date: 2026-06-02

Status: historical diagnostic. Use this only for root-cause lessons; current
release evidence lives in `v02-evidence-ledger.md`.

## Signal

Focused cleaned-runtime trace: `13` examples, `2` correct, `11` wrong, `0`
errors, `0` timeouts. Later generic slices improved this to `4/13`, with the
remaining misses dominated by metrics, span roles, and unsafe model fallback.

Broad cleaned BIRD remained unhealthy (`5/100` first-100 diagnostic), so BIRD is
research signal for this line, not the current release gate.

Raw replay paths are retained under:

- `artifacts/results-json/docs-results/v02-bird-failure-trace-cleaned-v1-report.json`
- `docs/results/v02-bird-first100-after-focused13.md`
- `docs/results/v02-focused13-full-recovery-slice.md`

## Lessons Kept

- Missing metric frames: rates, percentages, ratios, excellence rates, age gaps,
  and "per X" questions need typed numerator/denominator evidence.
- Span-role confusion: field, metric, question, date, and value spans must be
  classified before predicate extraction.
- Incomplete frames: projections, predicates, joins, groups, order, and metrics
  must all be solved before SQL can be accepted.
- List queries should not add `DISTINCT` unless the question asks for unique
  values or cardinality evidence proves it safe.
- Rejected QueryFrames must fail closed unless a typed fallback proposes a legal
  plan; direct model SQL is not release evidence.

## Implemented Afterward

- Span-role promotion checks.
- Explicit `DISTINCT` safety.
- Multi-projection QueryFrame support.
- Schema-description CSV ingestion for opaque fields.
- BoundQueryPlan acceptance gate and typed fallback packet work.

## Current Action

Continue SemanticAtlas and BoundQueryPlan work. Do not reintroduce BIRD-family
shortcut routers, benchmark phrase maps, query-specific Rust fixes, or raw model
SQL acceptance.
