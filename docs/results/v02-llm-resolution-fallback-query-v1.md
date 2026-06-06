# LLM-Resolution Fallback Query Smoke v1

Date: 2026-06-04

## Summary

`llm-resolution-fallback-query` now proves the local application boundary for
typed fallback SQL candidates.

The command runs local `semsql query` first. If local routing fails closed, it
writes the rejected-query packet and OpenAI request preview, then applies only a
typed proposal that passes local SemSQL validation/rendering. It does not
execute SQL and it does not permit direct provider SQL.

Smoke command:

```bash
uv run python -m semsql_eval llm-resolution-fallback-query \
  --graph target/pathway_decision_benchmark_schema_alias_v9/graphs/growth_ops_semantic_alias.semsql \
  --question "Which support agent resolved the most high priority tickets?" \
  --out target/llm_resolution_fallback/schema_alias_pq004_v1 \
  --include-samples \
  --proposal-json target/llm_resolution_packets/schema_alias_v9/platform-pq004.proposal.json \
  --strict
```

Result:

- status: `selected`
- selected source: `typed_fallback`
- local routed: `False`
- local stage: `needs_model`
- provider: `none`
- provider calls: `0`
- direct LLM SQL used: `False`
- fallback render valid: `True`
- selected SQL execution result on generated SQLite fixture: `Jon Bell, 2`

Artifacts are intentionally under ignored `target/`:

- `target/llm_resolution_fallback/schema_alias_pq004_v1/query-frame.json`
- `target/llm_resolution_fallback/schema_alias_pq004_v1/rejected.packet.json`
- `target/llm_resolution_fallback/schema_alias_pq004_v1/openai-request.json`
- `target/llm_resolution_fallback/schema_alias_pq004_v1/render.json`
- `target/llm_resolution_fallback/schema_alias_pq004_v1/fallback-query.json`
- `target/llm_resolution_fallback/schema_alias_pq004_v1/fallback-query.md`

## Interpretation

This is the first usable fail-closed fallback wrapper. It still requires either
a preexisting typed proposal or an opt-in provider call, but the selected SQL
candidate is local-rendered and schema-validated. The remaining external proof
is whether a live provider can independently produce valid typed proposals for
the copied semantic-alias packets.
