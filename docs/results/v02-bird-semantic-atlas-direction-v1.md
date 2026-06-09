# v0.2 BIRD SemanticAtlas Direction
Date: 2026-06-08. Current direction note; numbers live in
[v02-evidence-ledger.md](v02-evidence-ledger.md).

## Decision
BIRD failures should be treated as SemanticAtlas coverage failures, not as a
reason to add benchmark-shaped physical tables, static query examples, or
direct SQL generation. The product path is:

`database -> DB-only atlas/codebook -> typed intent -> bound plan -> guarded SQL`.

Materialize the atlas once per database as queryable metadata tables or indexes
beside the `.semsql` graph. This gives BIRD the same pre-query analysis used for
customer databases without altering benchmark data.

## Benchmark Rule
For BIRD, build the same virtual atlas/codebook a real customer database would
get from DB-only evidence:

- table/field names, relationships, types, row counts, and active-table hints;
- bounded non-PII sample values and code-like value dictionaries;
- inferred field roles, display fields, metric candidates, and date roles;
- lexical and similarity lookup over entity, field, value, and metric aliases;
- provenance, type compatibility, relationship distance, and confidence for
  every candidate.

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

## Query-Time Retrieval
Use a staged linker rather than one global fuzzy table:

1. decompose the question into entity, projection, metric, predicate, grouping,
   ordering, date, and limit spans;
2. retrieve candidates independently for each role using exact aliases,
   normalized tokens, descriptions, bounded values, and semantic similarity;
3. expand only along proven relationship paths;
4. score complete bindings for type compatibility, value-field fit, role fit,
   co-location, and join cost;
5. render only a complete validated plan; otherwise clarify or send the bounded
   candidate set to typed fallback.

Similarity is candidate generation, not authority. A fuzzy value hit cannot
cross fields or invent a metric, relationship, or SQL shape.

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
- projection aliases are now consumed only from explicit output spans such as
  `list/show ... of/for/where`, so filter phrases do not promote predicate
  fields into the SELECT list.
- ranked metric-value requests strip rank words from the output span, bind the
  metric expression, prefer same-base categorical predicates, and filter NULL
  metric ratios before ordering.
- count queries with descriptive metric filters stay on the count route; local
  field-label context grounds numeric thresholds such as `average score in
  Math > 400` without treating `Math`/`SAT` as location values.
- explicit related numeric thresholds such as `number of test takers not more
  than 250` bind to the matching related numeric field instead of an unrelated
  base-table number field.
- high-confidence value candidates from samples/dictionaries must be selected
  or covered by a more specific/lifecycle-equivalent value; otherwise the plan
  fails closed instead of accepting a partial predicate set.
- unscoped duplicate text values across plausible fields now fail closed unless
  a field role or metric co-location disambiguates the selected field.
- those ambiguous duplicate-value bails now build `resolve_value_binding`
  packets with exact field-scoped candidates, evidence source, and relationship
  context, so provider fallback can choose/clarify without direct SQL.
- route proposals that keep the runtime-selected ambiguous field can be locally
  repaired when candidate field-label/value-neighborhood evidence is unique;
  follow-up ambiguous values can co-locate to the resolved entity. Broad subject
  entity words such as `schools` do not by themselves bind value fields.

The retained description-aware first50 checkpoint stayed at `3/50`. A targeted
slice after related-field, related-fact, output-span projection, ranked
metric-value, and metric-filter count work is `7/7`, wrong accepted SQL `0`,
bails `0`; first20 is `7/20`, wrong `0`, bailed `13`. The threshold exact
probe for indexes `16` and `18` now bails `2/2` with
`ambiguous_unscoped_value_field` instead of accepting wrong SQL. Index `18`
now packetizes `directly funded` as `schools.fundingtype` versus
`frpm.charter_funding_type`, and `Fresno` as the plausible county/name fields;
a route-shaped proposal that kept `schools.*` is repaired to
`frpm.charter_funding_type` plus co-located `frpm.county_name` and renders
through local guardrails in the CLI fallback path with provider `none`. Index
`16` remains unresolved with `value_binding_unresolved`, which is correct until
the atlas or a provider can disambiguate `Alameda`. The next root cause is
planner-side role binding for the remaining metric/value/group/order slots
using reusable query-time atlas/codebook candidates. Numeric metric-like scope
phrases are filtered out of value aliases, so phrases such as
`eligible free rate` surface as metric evidence rather than bogus count-field
values. A naive whole-query projection boost once regressed
`zip code ... charter schools`; current planner use is intentionally
slot/role-aware.

The captured-packet batch now resolves packets directly instead of rerunning
the cascade. On the current three BIRD ambiguity packets it completed in
`1.5s`: `1/3` selected locally, `2/3` unresolved, `0` provider calls, and `0`
direct SQL. The unresolved index `8` is a metric/ranking binding failure, not a
value-alias failure, so adding more lookup synonyms would be the wrong fix.
