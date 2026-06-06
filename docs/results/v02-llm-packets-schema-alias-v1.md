# Pathway LLM-Resolution Packets

- report: `artifacts\results-json\docs-results\v02-pathway-benchmark-schema-alias-v9.json`
- policy: `bound_plan`
- packets: `2`
- samples included: `False`

| Suite | Case | Family | Route Reason | Packet |
|---|---|---|---|---|
| `platform` | `pq004` | `topk_group_count` | `runtime_route_not_promoted:routed_grouped_aggregate` | `target\llm_resolution_packets\schema_alias_v9\platform-pq004.packet.json` |
| `platform` | `pq005` | `multi_join_group_avg` | `runtime_route_not_promoted:routed_grouped_aggregate` | `target\llm_resolution_packets\schema_alias_v9\platform-pq005.packet.json` |

## Typed Proposal Proof

Generated local-only proof artifacts:

- `target\llm_resolution_packets\schema_alias_v9\platform-pq004.proposal.json`
- `target\llm_resolution_packets\schema_alias_v9\platform-pq004.render.json`
- `target\llm_resolution_packets\schema_alias_v9\platform-pq005.proposal.json`
- `target\llm_resolution_packets\schema_alias_v9\platform-pq005.render.json`

Verification:

- `uv run python -m semsql_eval llm-resolution-render ... --strict` succeeds
  for both packets with `0` validation issues.
- Executing rendered SQL against
  `target\pathway_decision_benchmark_schema_alias_v9\platform\database\growth_ops_semantic_alias\growth_ops_semantic_alias.sqlite`
  matches the expected row values:
  - `pq004`: `Jon Bell, 2`
  - `pq005`: `Jon Bell, 9.5`

The rendered SQL uses local schema-bound expressions such as
`ORDER BY COUNT(...)` and `ORDER BY AVG(...)` instead of final SQL supplied by
an LLM. Runtime fallback integration is still pending.
