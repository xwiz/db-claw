# v0.2 Atlas-Backed Route Promotion
Date: 2026-06-07.

## Decision
Final SQL promotion now treats text predicate values as unsafe unless the SemanticAtlas provides value evidence through samples, enum vocabulary, or scope predicates. Source-free typed literals remain allowed only for compatible numeric, date/range, and boolean fields.

## Why
BIRD-style failures were not mainly "can we generate SQL text"; they were ungrounded binding failures. The practical production path is a virtual semantic lookup layer, not physical benchmark tables and not string guessing.

## Verification
- `cargo test -p semsql-runtime --locked bound_query_plan --no-fail-fast`
- `cargo test -p semsql-runtime --locked graph_schema_atlas_tests --no-fail-fast`

Partial BIRD checkpoint:

- artifact: `target/v02/current-bird100-atlas-gated-v1/report.json`
- completed: `50/100` before manual stop after shell timeout
- product safety: final SQL `28`, final wrong `26`, route-used wrong `6`, model-after-reject wrong `13`
- interpretation: evidence gating reduces accepted output volume, but BIRD remains blocked by metric/rank/grade-span/relationship binding and typed fallback.

## Next
Route `missing_value_evidence` into bounded lookup/typed LLM proposals, then rerun BIRD100 and compare accepted-wrong-SQL counts.
