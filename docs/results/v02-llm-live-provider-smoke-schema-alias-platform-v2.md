# LLM-Resolution Provider Batch

- provider: `openai`
- packet dir: `target\llm_resolution_live_smoke\openai_preview_v1\schema_alias_packets`
- proposal dir: `target\llm_resolution_live_smoke\openai_provider_v7\schema_alias_platform_proposals`
- provider output dir: `target\llm_resolution_live_smoke\openai_provider_v7\schema_alias_platform_provider`
- render output dir: `target\llm_resolution_live_smoke\openai_provider_v8\schema_alias_platform_render`
- dialect: `sqlite`
- packets: `2`
- provider calls: `0`
- existing proposals reused: `2`
- valid: `2`
- invalid: `0`
- missing proposals: `0`
- provider errors: `0`

| Case | Provider Called | Existing Proposal | Valid | Issues | Provider Error | Render |
|---|---:|---:|---:|---:|---|---|
| `platform-pq004` | `False` | `True` | `True` | `0` | `-` | `target\llm_resolution_live_smoke\openai_provider_v8\schema_alias_platform_render\platform-pq004.render.json` |
| `platform-pq005` | `False` | `True` | `True` | `1` | `-` | `target\llm_resolution_live_smoke\openai_provider_v8\schema_alias_platform_render\platform-pq005.render.json` |

## Execution Check

- SQLite fixture:
  `target/pathway_decision_benchmark_schema_alias_v9/platform/database/growth_ops_semantic_alias/growth_ops_semantic_alias.sqlite`
- `platform-pq004`: rendered SQL returned `Jon Bell, 2`.
- `platform-pq005`: rendered SQL returned `Jon Bell, 9.5`.
- `platform-pq005` issue is warning
  `clarify_auto_promoted_hidden_filter`: the provider proposal was otherwise
  valid but asked whether to add an unstated status filter. Local policy
  rendered the explicit typed plan and kept the warning in the artifact.
