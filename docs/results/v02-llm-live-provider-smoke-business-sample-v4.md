# LLM-Resolution Provider Batch

- provider: `openai`
- packet dir: `target\llm_resolution_live_smoke\openai_preview_v2\business_sample_packets`
- proposal dir: `target\llm_resolution_live_smoke\openai_provider_v8\business_sample_proposals`
- provider output dir: `target\llm_resolution_live_smoke\openai_provider_v8\business_sample_provider`
- render output dir: `target\llm_resolution_live_smoke\openai_provider_v11\business_sample_render`
- dialect: `sqlite`
- packets: `3`
- provider calls: `0`
- existing proposals reused: `3`
- valid: `3`
- invalid: `0`
- missing proposals: `0`
- provider errors: `0`

| Case | Provider Called | Existing Proposal | Valid | Issues | Provider Error | Render |
|---|---:|---:|---:|---:|---|---|
| `business-ba004` | `False` | `True` | `True` | `1` | `-` | `target\llm_resolution_live_smoke\openai_provider_v11\business_sample_render\business-ba004.render.json` |
| `business-ba017` | `False` | `True` | `True` | `0` | `-` | `target\llm_resolution_live_smoke\openai_provider_v11\business_sample_render\business-ba017.render.json` |
| `business-ba019` | `False` | `True` | `True` | `1` | `-` | `target\llm_resolution_live_smoke\openai_provider_v11\business_sample_render\business-ba019.render.json` |

## Execution And Policy Notes

- SQLite fixture:
  `target/pathway_decision_benchmark_business_schema_alias_v5/business/database/business_analytics_semantic_alias/business_analytics_semantic_alias.sqlite`
- `business-ba004`: rendered SQL returned `1`.
- `business-ba017`: rendered SQL returned `Omar Diaz, 2`.
- `business-ba019`: rendered SQL returned grouped conversion rates:
  `Partner Push, 100.0`; `Field Event, 100.0`; `Spring Webinar, 50.0`;
  `February Search, 33.333333333333336`.
- `business-ba004` warning:
  `clarify_auto_promoted_subject_date_anchor`. Local policy added
  `prospects.captured_on` because the provider asked a date-anchor question and
  the single target entity had exactly one date field.
- `business-ba019` warning:
  `clarify_auto_promoted_metric_catalog`. Local metric policy resolved
  lead/prospect conversion rate to packet-backed
  `prospects.status = converted`, removed the ambiguous customer-lifecycle
  join, and rendered the grouped conditional-rate metric locally.
