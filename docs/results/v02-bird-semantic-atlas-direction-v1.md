# v0.2 BIRD SemanticAtlas Direction
Date: 2026-06-08. Current direction note; numbers live in
[v02-evidence-ledger.md](v02-evidence-ledger.md).

## Decision
BIRD failures should be treated as SemanticAtlas coverage failures, not as a
reason to add benchmark-shaped physical tables, static query examples, or
direct SQL generation. The product path is:

`database -> DB-only atlas/codebook -> typed intent -> bound plan -> guarded SQL`.

## Benchmark Rule
For BIRD, build the same virtual atlas/codebook a real customer database would
get from DB-only evidence:

- table/field names, relationships, types, row counts, and active-table hints;
- bounded non-PII sample values and code-like value dictionaries;
- inferred field roles, display fields, metric candidates, and date roles;
- similarity lookup over entity, field, value, and metric aliases.

Do not use dev gold SQL, per-question examples, or BIRD-specific table-name
maps to make a case pass.

This means "create tables like real database examples" should be interpreted as
virtual SemanticAtlas tables, not new physical benchmark tables. A lookup table
is valid only when it is field-scoped, provenance-tagged, and derived from
schema descriptions, relationships, bounded samples, framework/source
vocabulary, or user-approved business definitions.

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
- related predicate fields with stronger label/value evidence are promoted to
  the related table instead of accepting a weaker same-table shortcut.
- related fact-table metrics are accepted when the prompt names a related
  dimension entity but the atlas proves all grounded measure/filter evidence
  belongs on the fact table.

The retained description-aware first50 checkpoint stayed at `3/50`. A targeted
slice after related-field and related-fact promotion is `5/5`, wrong accepted
SQL `0`, bails `0`; first20 is `5/20`, wrong `0`, bailed `15`. The next root
cause is not another route patch; it is role-aware consumption of the reusable
query-time atlas/codebook candidate API over value dictionaries, field roles,
active tables, metric candidates, and fast graph loading. A naive projection
boost from codebook aliases regressed `zip code ... charter schools` to a
filter-field projection, so candidate use must be slot/role-aware.
