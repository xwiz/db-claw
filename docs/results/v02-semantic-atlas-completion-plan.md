# v0.2 SemanticAtlas Completion Plan
Date: 2026-06-08. Active loop only; numbers live in [v02-evidence-ledger.md](v02-evidence-ledger.md).

## Spine
`SemanticGraph -> SemanticAtlas -> IntentFrame -> BoundQueryPlan -> local SQL renderer -> validator -> optional read-only execution`.
LLMs may propose typed plans over bounded evidence; direct provider SQL is never accepted.

## Next Loop
1. Gate broad evals on accepted-wrong-SQL diagnostics before interpreting accuracy.
2. Treat BIRD misses as DB-only atlas/codebook gaps: enrich relationships, active tables, display fields, values, metrics, dates, and similarity lookup without dev gold SQL or benchmark maps.
3. Route `missing_value_evidence` rejects to lookup/typed LLM proposals, not direct SQL.
4. Convert fail-closed or newly emitted BIRD/real-schema cases into grounded plans by improving join/table selection, projection intent, and filter-vs-output separation over atlas/codebook evidence.
5. Probe the next real app/schema with the alpha package path for source vocab, metrics, dates, live table-selection evidence, and fail-closed rejects.
6. Capture unresolved cases as typed fallback packets, not static runtime routes.
7. Reduce cold graph load/startup latency; current BIRD graph CLI probes can exceed production-friendly timings even when Stage 0a work is tiny.
8. Tighten metric catalogs, active-table ranking, and date/value normalization from private alpha and BIRD stress evidence.
9. Keep strict production-readiness aggregation green before any wider release.

## Backlog
Metric catalogs; live row-count/table-selection enrichment; shard/date/PII/tenant hints; BI/customer analytics frames;
result-shape hints; BIRD DB-only atlas enrichment; broader real-app probes; richer diagnostics.

## Stoplight
- Green: `0` wrong accepted SQL, fail-closed non-routes, local provider validation.
- Red: static shortcuts, direct SQL, partial-plan rendering, curated-suite claims, or Stage 3 repairs outside legal candidates.
