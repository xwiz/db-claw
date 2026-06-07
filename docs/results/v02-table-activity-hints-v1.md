# v0.2 Table Activity Hints
Date: 2026-06-07.

## Change
SchemaCard and runtime SemanticAtlas traces now expose graph-derived table activity hints:

- non-PII sample-value field/value counts;
- field-scoped value-dictionary counts;
- relationship degree;
- display/date/status/numeric role counts;
- physical-family role when a base/partition family is detected.

These hints are metadata only. They do not include live row counts, do not select physical partitions, and do not authorize SQL generation after an ambiguous route.

## Verification
- `uv run pytest python\semsql_eval\tests\test_llm_resolution.py`
- `uv run ruff check python`
- `uv run mypy python`
- `cargo test -p semsql-runtime --locked semantic_atlas_trace`
- `cargo clippy -p semsql-runtime --all-targets --locked -- -D warnings`
- `python scripts/audit_static_query_shortcuts.py`

## Follow-up
Use DB-probe evidence to populate live row-count/table-selection enrichment separately, then feed it into typed fallback packets and BoundQueryPlan candidate generation.
