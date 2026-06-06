# v0.2 SemanticAtlas Completion Plan
Date: 2026-06-06. Active loop only; numbers live in [v02-evidence-ledger.md](v02-evidence-ledger.md).

## Spine
`SemanticGraph -> SemanticAtlas -> IntentFrame -> BoundQueryPlan -> local SQL renderer -> validator -> optional read-only execution`.
LLMs may propose typed plans over bounded evidence; direct provider SQL is never accepted.

## Next Loop
1. Publish real pre-release npm packages plus GitHub binary assets.
2. Run public package smoke, then strict production-readiness aggregation.
3. Probe the next real app/schema for source vocab, metrics, dates, active-table
   hints, and fail-closed rejects.
4. Capture unresolved cases as typed fallback packets, not static runtime routes.

## Backlog
Metric catalogs; shard/date/PII/tenant hints; BI/customer analytics frames;
result-shape hints; broader real-app probes; richer diagnostics.

## Stoplight
- Green: `0` wrong accepted SQL, fail-closed non-routes, local provider validation.
- Red: static shortcuts, direct SQL, partial-plan rendering, curated-suite claims,
  or Stage 3 repairs outside legal candidates.
