# LLM-Resolution Render Batch

- packet dir: `target\llm_resolution_live_smoke\openai_preview_v1\business_schema_alias_packets`
- proposal dir: `target\llm_resolution_live_smoke\openai_provider_v12\business_full_proposals`
- output dir: `target\llm_resolution_live_smoke\openai_provider_v15\business_full_render`
- dialect: `sqlite`
- packets: `11`
- valid: `11`
- invalid: `0`
- missing proposals: `0`

| Case | Valid | Issues | Missing Proposal | Render |
|---|---:|---:|---:|---|
| `business-ba003` | `True` | `1` | `False` | `target\llm_resolution_live_smoke\openai_provider_v15\business_full_render\business-ba003.render.json` |
| `business-ba004` | `True` | `1` | `False` | `target\llm_resolution_live_smoke\openai_provider_v15\business_full_render\business-ba004.render.json` |
| `business-ba005` | `True` | `1` | `False` | `target\llm_resolution_live_smoke\openai_provider_v15\business_full_render\business-ba005.render.json` |
| `business-ba006` | `True` | `1` | `False` | `target\llm_resolution_live_smoke\openai_provider_v15\business_full_render\business-ba006.render.json` |
| `business-ba010` | `True` | `0` | `False` | `target\llm_resolution_live_smoke\openai_provider_v15\business_full_render\business-ba010.render.json` |
| `business-ba011` | `True` | `1` | `False` | `target\llm_resolution_live_smoke\openai_provider_v15\business_full_render\business-ba011.render.json` |
| `business-ba012` | `True` | `1` | `False` | `target\llm_resolution_live_smoke\openai_provider_v15\business_full_render\business-ba012.render.json` |
| `business-ba014` | `True` | `0` | `False` | `target\llm_resolution_live_smoke\openai_provider_v15\business_full_render\business-ba014.render.json` |
| `business-ba017` | `True` | `0` | `False` | `target\llm_resolution_live_smoke\openai_provider_v15\business_full_render\business-ba017.render.json` |
| `business-ba019` | `True` | `1` | `False` | `target\llm_resolution_live_smoke\openai_provider_v15\business_full_render\business-ba019.render.json` |
| `business-ba020` | `True` | `1` | `False` | `target\llm_resolution_live_smoke\openai_provider_v15\business_full_render\business-ba020.render.json` |

## Execution Check

- provider calls in this rerender: `0`
- direct LLM SQL used: `0`
- valid SQL executed on generated SQLite fixture: `11/11`
- execution errors: `0`

Representative rows:

- `business-ba003`: `Noah Smith, 230000.0`; `Mia Chen, 200000.0`
- `business-ba010`: `Northstar Health`
- `business-ba012`: `Vector Retail, smb, 24000.0`
- `business-ba019`: `Partner Push, 100.0`; `Field Event, 100.0`; `Spring Webinar, 50.0`; `February Search, 33.333333333333336`

New generic promotions in this run:

- `clarify_auto_promoted_open_stage_metric`: resolves open pipeline by excluding packet-backed terminal stage values such as `closed_won` and `closed_lost`.
- `clarify_auto_promoted_lifecycle_event_date`: resolves lifecycle/date clarifications by preferring a related event/status table with a packet-backed lifecycle value and event date field.
