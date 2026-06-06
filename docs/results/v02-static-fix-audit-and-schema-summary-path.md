# v0.2 Static-Fix Audit And Schema-Summary Path

Date: 2026-06-05

Retained decision memo. Current gate numbers live in
`v02-evidence-ledger.md`.

## Decision

Do not grow runtime phrase tables to recover benchmark or app examples.

Runtime code should hold generic grammar, routing, scoring, typed planning,
rendering, and fail-closed safety. Database meaning should come from graph
evidence:

- schema identifiers, labels, and descriptions;
- relationships and inferred relationship candidates;
- sampled non-PII values;
- framework/application vocabulary;
- field-scoped value dictionaries;
- metric formulas and approved atlas entries;
- typed LLM proposals for rejected cases.

If a fix needs an app phrase hardcoded in Rust, it belongs in extraction,
framework vocab, a metric catalog, or user-approved atlas metadata.

## What Changed

- DB extraction can derive field-scoped `scope_predicate` vocabulary from
  schema/value descriptions.
- Rejected-query packets include bounded graph evidence and value dictionaries.
- Typed fallback validation checks fields, operators, value compatibility,
  grouping, and unsafe shapes before any renderer sees a proposal.
- Rendering remains local; providers do not emit executable SQL.

## Proof To Preserve

California-schools described-schema extraction produced:

- `3` entities, `89` fields, `2` relationships;
- `277` vocabulary rows;
- `118` schema-value predicates.

Representative field-scoped predicates:

- `charter` -> `schools.charter = 1`;
- `not charter` -> `schools.charter = 0`;
- `magnet` -> `schools.magnet = 1`;
- `active` -> `schools.statustype = Active`;
- `state special schools` -> `schools.doc = 31`.

Runtime spot checks used those predicates without a runtime phrase-table
shortcut. A grouped typed fallback proposal for active charter/magnet schools
by county validated and rendered locally.

## Tests

- `cargo test -p semsql-cli db_only_extraction_derives_scope_predicates_from_value_descriptions -- --nocapture`
- `cargo test -p semsql-cli -- --nocapture`
- `cargo clippy -p semsql-cli --features onnx -- -D warnings`

## Next

1. Extend field-scoped predicates into BI/ops frames.
2. Treat sampled DB values as bounded field-scoped evidence.
3. Keep `BoundQueryPlan` as the only executable boundary.
4. Expand typed fallback coverage for dates, aggregate ordering, aliases,
   `DISTINCT`, null predicates, anti-joins, and governed expression metrics.
