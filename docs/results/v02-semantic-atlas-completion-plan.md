# v0.2 SemanticAtlas Completion Plan
Date: 2026-06-08. Active loop only; numbers live in [v02-evidence-ledger.md](v02-evidence-ledger.md).

## Spine
`SemanticGraph -> SemanticAtlas -> IntentFrame -> BoundQueryPlan -> local SQL renderer -> validator -> optional read-only execution`.
LLMs may propose typed plans over bounded evidence; direct provider SQL is never accepted.

## Next Loop
1. Gate broad evals on accepted-wrong-SQL diagnostics before interpreting accuracy.
2. Make Laravel the reference application-aware integration: exact ORM relationships/keys, scopes, casts, accessors, validation/API resources, Filament filters, and safe report/query-builder shapes.
3. Add a source-controlled authored semantic contract for approved aliases, virtual fields, metrics, join paths, table rules, and typed plan templates.
4. Add operational resolution memory with schema/source drift keys and provisional, confirmed, governed, stale, and rejected states.
5. Persist a DB-profile atlas per graph for raw DB bootstrap: aliases, descriptions, roles, relationships, bounded values, statistics, metrics, dates, activity, provenance, and confidence.
6. Retrieve candidates per intent role and enumerate complete plans. Similarity proposes candidates; application evidence, type/value compatibility, relationship paths, and validation decide.
7. Route missing or ambiguous plans to bounded lookup/typed LLM proposals or clarification, never direct SQL.
8. Evaluate held-out real-app questions with app-aware versus DB-only ablations, then port the contract to Django, Rails, Prisma, and Drizzle.
9. Keep BIRD as a raw-DB stress test and reduce cold graph load, but do not let it displace application-aware product work.
10. Keep strict production-readiness aggregation green before any wider release.

## Backlog
Metric catalogs; live row-count/table-selection enrichment; shard/date/PII/tenant hints; BI/customer analytics frames;
result-shape hints; BIRD DB-only atlas enrichment; broader real-app probes; richer diagnostics.

## Stoplight
- Green: `0` wrong accepted SQL, fail-closed non-routes, local provider validation.
- Red: static shortcuts, direct SQL, partial-plan rendering, curated-suite claims, or Stage 3 repairs outside legal candidates.
