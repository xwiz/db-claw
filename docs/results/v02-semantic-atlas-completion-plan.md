# v0.2 SemanticAtlas Completion Plan
Date: 2026-06-09. Active loop; numbers live in [the ledger](v02-evidence-ledger.md).

## Spine
`SemanticGraph -> SemanticAtlas -> AtlasStrength -> IntentFrame -> CandidatePlans -> BoundQueryPlan -> ResolutionDecision`.
LLMs propose typed plans over bounded evidence; direct provider SQL is never authority.

## Implemented
- Four-way public decision with slot strength and bounded candidates.
- JSON packets plus local web, CLI, and JSON resolution surfaces.
- Authored alias/metric contracts and drift-keyed confirmed/governed memory.
- Approved enum corrections outrank weaker generated/sample evidence.

## Next
1. Extend contracts to virtual fields, canonical joins, date roles, table-family rules, and typed templates.
2. Complete Laravel relationships, scopes, casts, accessors, validation/resources, Filament filters, and report shapes.
3. Run Laravel extract/query/resolve/save/rerun acceptance.
4. Add memory rejection/promotion workflows and keep drift visible in `doctor`.
5. Visually QA desktop/mobile resolver and document JSON embedding.
6. Run held-out app-aware versus DB-only ablations, then port to other frameworks.
7. Keep BIRD as raw-DB stress research and all private-alpha gates green.

## Stoplight
- Green: `0` wrong accepted SQL, fail-closed non-routes, locally validated proposals.
- Red: static shortcuts, direct SQL, partial-plan rendering, or curated-suite claims.
