# v0.2 Application-Context-First Direction

Date: 2026-06-09. Product and implementation direction based on current code
and retained evidence.

## Decision

SemanticSQL should lead with application-aware extraction. ORM, framework,
API, UI, and report code are often the closest thing an application has to a
semantic specification. Raw database analysis remains a lower-confidence
bootstrap path.

The product spine becomes:

`application contract + DB facts + approved memory -> typed plan -> guarded SQL`

Raw DB mode becomes:

`DB profile -> candidate contract -> bounded experiment/clarify -> approved memory`

## Evidence

- Laravel `mailer_web` grounded `3765/3765` source vocabulary rows over
  `214` entities; `fraudv` grounded `288/288`; the Next.js/Drizzle probe
  grounded `174/174`.
- The MariaDB `53/53` and broader BI/ops `46/46` results are generated,
  schema-derived capability probes. They prove safe plan execution after the
  intended fields are expressible, not arbitrary end-user accuracy.
- BIRD remains weak without application semantics: first20 `7/20` after
  fail-closed cleanup, and retained broad checkpoints remain poor.
- SemanticGraph schema v5 stores vocabulary, relationships, samples, conflicts,
  and metrics, but no approved bindings, plan templates, corrections, or query
  outcomes.
- `semsql doctor --write-overrides` writes a scaffold, but no runtime or
  extractor reads `semsql.overrides.yaml`.
- Query-time codebook lookup is lexical/token-overlap scoring over generated
  aliases. It has no persisted semantic retrieval index or feedback memory.

## Current Extraction Gaps

The existing adapters provide a good base but do not yet extract the full
application contract:

- Laravel: no ORM relationship methods/keys, pivots, morphs, accessors,
  computed-field lineage, FormRequest rules, API Resources, policies, saved
  report/query-builder shapes, or Filament filters.
- Next.js: Prisma `@relation` and Drizzle `references()` are deliberately
  skipped.
- Django/Rails: important relationship, scope, serializer, enum, and report
  surfaces remain partial.
- Extracted casts are collected by Laravel but are not represented as governed
  field-role/type evidence in the graph contract.

## Target Contract

Keep generated evidence and mutable learning separate.

1. Generated SemanticGraph:
   entities, physical fields, exact relationships, labels, enums, scopes,
   types, sensitivity, metrics, UI/report projections, and provenance.
2. Authored semantic contract:
   reviewed aliases, virtual-field mappings, business metrics, canonical join
   paths, active-table rules, and reusable plan templates. Store this in a
   source-controlled additive file and ingest it at the highest authority.
3. Operational resolution memory:
   query outcomes, candidate plans, clarification choices, and corrections.
   Keep it in a mutable sidecar/store; promote reviewed facts into the authored
   contract.

Never persist a mapping merely because SQL executed. Promotion requires source
proof, explicit approval, a trusted saved report/query, or repeated confirmed
outcomes. All learned facts are bound to application/schema hashes and become
stale on drift.

## Query Loop

1. Parse the question into typed roles.
2. Retrieve application-contract candidates first, then DB-profile candidates.
3. Enumerate a small set of complete plans, not independent field guesses.
4. Validate types, values, joins, scopes, safety, and result shape.
5. Use bounded read-only probes or typed LLM help only when evidence is
   incomplete.
6. Clarify when candidates remain meaningfully different.
7. Save the confirmed binding or plan template with provenance and drift keys.

Memory should store normalized intent/slot templates rather than final SQL, so
new values and dates reuse the learned path without copying stale literals.

## Implementation Order

1. Make Laravel the reference integration through a Composer/Artisan bridge
   that combines runtime framework metadata with the existing static extractor.
2. Add exact Eloquent relationships and keys, local/global scopes, casts,
   accessors, FormRequest/API Resource fields, Filament filters, and safe
   query-builder/report shape extraction.
3. Add authored contract ingestion and real override consumption.
4. Add versioned resolution memory with provisional, confirmed, governed,
   stale, and rejected states.
5. Add candidate-plan experimentation for raw DBs using column types/lengths,
   cardinality, bounded values, FKs, indexes, table roles, and read-only probes.
6. Add typed onboarding assistance: an LLM may propose a contract once, but
   local validation and approval own promotion.
7. Port the proven contract to Django, Rails, Prisma, and Drizzle.

## Evaluation

Replace generated-family scores as the headline accuracy claim with held-out
real-app evaluations:

- app-aware versus DB-only ablation on the same questions;
- extraction coverage for relationships, scopes, values, metrics, and virtual
  fields;
- candidate recall at k before planning;
- first-attempt plan correctness and accepted-wrong-SQL;
- clarification rate;
- confirmed-memory reuse accuracy after paraphrase/value/date changes;
- drift invalidation correctness.

BIRD remains useful as a raw-DB stress test, but it should not set the primary
product roadmap.
