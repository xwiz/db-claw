# v0.2 SemanticAtlas Completion Plan
Date: 2026-06-08. Active loop only; numbers live in [v02-evidence-ledger.md](v02-evidence-ledger.md).

## Spine
`SemanticGraph -> SemanticAtlas -> IntentFrame -> BoundQueryPlan -> local SQL renderer -> validator -> optional read-only execution`.
LLMs may propose typed plans over bounded evidence; direct provider SQL is never accepted.

## Next Loop
1. Gate broad evals on accepted-wrong-SQL diagnostics before interpreting accuracy.
2. Persist a DB-only atlas index per graph: aliases, descriptions, field roles, relationships, bounded values, value dictionaries, metric candidates, date roles, activity hints, provenance, and confidence.
3. Add hybrid exact/lexical/similarity retrieval per intent role. Similarity proposes candidates; field type, value compatibility, relationship paths, and plan validation decide.
4. Route missing or ambiguous bindings to bounded lookup/typed LLM proposals, not direct SQL.
5. Convert fail-closed BIRD/real-schema cases into grounded plans by improving join/table selection, projection intent, fact/dimension metric handling, and filter/output separation over atlas candidates.
6. Add metric/ranking role binding for top-by-one-measure/project-another, grouped top-k, ordinal ranges, and metric filters with separate projections.
7. Probe the next real app/schema with the alpha package path for source vocab, metrics, dates, live table-selection evidence, and fail-closed rejects.
8. Capture unresolved cases as typed fallback packets, not static runtime routes.
9. Reduce cold graph load/startup latency; current BIRD graph CLI probes can exceed production-friendly timings even when Stage 0a work is tiny.
10. Keep strict production-readiness aggregation green before any wider release.

## Backlog
Metric catalogs; live row-count/table-selection enrichment; shard/date/PII/tenant hints; BI/customer analytics frames;
result-shape hints; BIRD DB-only atlas enrichment; broader real-app probes; richer diagnostics.

## Stoplight
- Green: `0` wrong accepted SQL, fail-closed non-routes, local provider validation.
- Red: static shortcuts, direct SQL, partial-plan rendering, curated-suite claims, or Stage 3 repairs outside legal candidates.
