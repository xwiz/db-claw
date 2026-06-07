# v0.2 Virtual SemanticAtlas Tables v1
Date: 2026-06-07. Retained implementation note; live status remains in
[v02-current-status.md](v02-current-status.md).

## Scope

Added generic runtime SemanticAtlas evidence that makes raw dynamic databases
look more like real app schemas without introducing benchmark-specific tables or
query shortcuts.

Runtime trace additions:

- `semantic_atlas.schema_version = 2`
- `semantic_atlas.value_aliases`: field-scoped aliases derived from bounded
  non-sensitive `sample_values` and `scope_predicate` vocabulary rows
- `semantic_atlas.metric_candidates`: governed metrics from graph
  `metric_definitions` plus derived numeric measure candidates

The CLI rejection/query-frame packet cap now bounds `value_aliases` and
`metric_candidates` alongside entities, fields, and relationships.

## Why

BIRD100 showed that raw table/field names plus Stage 3 guesses are not enough.
The next generalizable path is a virtual atlas: value dictionaries, metric
views, field roles, and entity summaries derived from DB metadata, samples,
comments, vocabulary, and governed metric definitions.

## Verification

```powershell
cargo test -p semsql-runtime --locked semantic_atlas --no-fail-fast
cargo test -p semsql-cli --locked query_frame_diagnostics_cap_large_semantic_atlas_arrays --no-fail-fast
```

Focused result:

- runtime SemanticAtlas tests: `5` passed
- CLI packet cap test: `1` passed

## Next

Use these virtual tables in typed fallback and route promotion:

- require field-scoped value evidence before value-bearing SQL is accepted;
- prefer governed metric candidates for rate/ratio phrases;
- reject or escalate when a route needs values/metrics absent from the atlas;
- rerun BIRD100 after fail-closed promotion uses this evidence.
