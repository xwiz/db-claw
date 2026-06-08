# v0.2 BIRD SemanticAtlas Direction
Date: 2026-06-08. Current direction note; numbers live in
[v02-evidence-ledger.md](v02-evidence-ledger.md).

## Decision
BIRD failures should be treated as SemanticAtlas coverage failures, not as a
reason to add benchmark-shaped tables, static query examples, or direct SQL
generation. The product path is:

`database -> DB-only atlas/codebook -> typed intent -> bound plan -> guarded SQL`.

## Benchmark Rule
For BIRD, build the same atlas a real customer database would get from DB-only
evidence:

- table/field names, relationships, types, row counts, and active-table hints;
- bounded non-PII sample values and code-like value dictionaries;
- inferred field roles, display fields, metric candidates, and date roles;
- similarity lookup over entity, field, value, and metric aliases.

Do not use dev gold SQL, per-question examples, or BIRD-specific table-name
maps to make a case pass.

## Production Rule
For real databases, enrich the atlas with app/framework/source vocabulary and
user-approved business definitions when available. When the atlas cannot ground
a value, metric, or join, emit a typed resolution packet for lookup/LLM/clarify.
Provider output may propose a bounded plan; direct provider SQL remains
rejected.

## Current Runtime Step
The runtime now distinguishes these generic evidence cases that BIRD exposed:

- id-like numeric literals can use entity-role phrases such as `event No. 354`;
- numeric thresholds can be justified by a selected route measure, such as
  `SUM(amount)` with `amount > 50000`.
- description-backed scope predicates are available in BIRD graph caches;
- date-role scoring prefers `OpenDate`/start fields for opened/created prompts;
- scope predicate matching handles field-scoped variants such as
  `direct charter-funded` -> `directly funded`.
- projection intent now separates requested output fields from predicate-only
  fields, so `phone numbers ... opened after ...` projects phone while using
  the date only as a filter.
- related predicate fields with stronger label/value evidence now fail closed
  instead of accepting a weaker same-table shortcut.

The retained description-aware first50 checkpoint stayed at `3/50`. A targeted
slice after the projection fix is `3/4`, wrong accepted SQL `0`, with one
intentional `ambiguous_related_predicate_field` bail. The next root cause is
join/table selection over available atlas evidence, not missing value aliases.
Also treat cold graph load/startup latency as a production-readiness blocker.
