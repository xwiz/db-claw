# LLM-Resolution Capture Query Smoke v1

Date: 2026-06-03

## Summary

`llm-resolution-capture-query` now turns one fail-closed runtime query into the
typed LLM handoff artifacts without contacting a provider.

Smoke command:

```bash
uv run python -m semsql_eval llm-resolution-capture-query \
  --graph target/pathway_decision_benchmark_schema_alias_v9/graphs/growth_ops_semantic_alias.semsql \
  --question "Which support agent resolved the most high priority tickets?" \
  --out target/llm_resolution_capture/schema_alias_pq004_v1 \
  --include-samples \
  --proposal-json target/llm_resolution_packets/schema_alias_v9/platform-pq004.proposal.json \
  --strict-render
```

Result:

- routed locally: `False`
- stage: `needs_model`
- query-frame captured: `True`
- rejected packet written: `True`
- strict OpenAI request preview written: `True`
- provider calls: `0`
- model preview: `gpt-5.2`
- local render with existing typed proposal: `valid`, `0` validation issues
- rendered SQL execution result on generated SQLite fixture: `Jon Bell, 2`

Artifacts are intentionally under ignored `target/`:

- `target/llm_resolution_capture/schema_alias_pq004_v1/query-frame.json`
- `target/llm_resolution_capture/schema_alias_pq004_v1/rejected.packet.json`
- `target/llm_resolution_capture/schema_alias_pq004_v1/openai-request.json`
- `target/llm_resolution_capture/schema_alias_pq004_v1/capture.json`
- `target/llm_resolution_capture/schema_alias_pq004_v1/capture.md`
- `target/llm_resolution_capture/schema_alias_pq004_v1/render.json`

## Interpretation

This closes the manual runtime handoff gap for rejected queries and proves the
captured packet can flow into the existing local typed renderer. The local
router still owns the first decision. The LLM is only given a typed SchemaCard
packet and must return a structured proposal; it is not allowed to emit final
SQL. Live provider validation and applying validated proposals back into the
runtime fallback remain separate gated steps.
