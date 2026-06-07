# v0.2 Rejection Packet Physical Family Evidence

Date: 2026-06-06. Retained evidence report; status lives in
[v02-current-status.md](v02-current-status.md).

Rejected-query packets now surface detected physical table families, for
example a base table plus partition-like members. This does not route SQL. It
keeps ambiguous physical tables fail-closed while giving typed fallback/review a
bounded reason to ask for app metadata or clarification.

The Rust CLI packet and Python LLM-resolution SchemaCard now share the
`physical_table_families` contract. The older `shard_families` alias remains for
historical artifact compatibility.

Verification:
- `cargo test -p semsql-cli --locked`
- `uv run pytest python\semsql_eval\tests\test_llm_resolution.py -k "schema_card_summarizes_graph_and_shard_family or rejected_query_packet_marks_ambiguous_family or resolution_proposal_rejects_ambiguous_shard_family_route or cli_style_physical_family_packet"`
- `uv run ruff check python\semsql_eval\src\semsql_eval\llm_resolution.py python\semsql_eval\tests\test_llm_resolution.py`
- `python scripts/audit_static_query_shortcuts.py`
- `python scripts/check_docs_hygiene.py --fail-current-looking --fail-unregistered-current-looking --fail-large-retained --fail-missing-historical-banner --top 12`

Key assertion: `ambiguous_physical_families_mentioned` is populated for a
question touching a detected table family, and `requires_clarification` remains
`true` when only the base family is mentioned.
