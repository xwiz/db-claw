# LLM-Resolution Fallback Semantic-Alias 13 v1

Date: 2026-06-04

## Summary

The local-first fallback batch wrapper was run over all `13` existing
semantic-alias false-negative packets that already had typed proposal proof
artifacts.

Inputs:

- `target/llm_resolution_packets/schema_alias_v9/*.packet.json`
- `target/llm_resolution_packets/schema_alias_v9/*.proposal.json`
- `target/llm_resolution_packets/business_schema_alias_v5/*.packet.json`
- `target/llm_resolution_packets/business_schema_alias_v5/*.proposal.json`

Output root:

- `target/llm_resolution_fallback_batch/schema_alias_v9/`
- `target/llm_resolution_fallback_batch/business_schema_alias_v5/`

Retained batch summaries:

- `v02-llm-resolution-fallback-batch-schema-alias-v1.md`
- `v02-llm-resolution-fallback-batch-business-schema-alias-v1.md`

Results:

- cases: `13` (`2` platform + `11` BI/customer analytics)
- selected: `13/13`
- selected source: `typed_fallback` for `13/13`
- provider calls: `0`
- direct LLM SQL used: `False` for `13/13`
- fallback render valid: `13/13`
- selected SQL execution smoke: `13/13` runnable on generated SQLite fixtures

## Execution Smoke

| Case | Rows | First Row |
|---|---:|---|
| `business-ba003` | `2` | `Noah Smith, 230000.0` |
| `business-ba004` | `1` | `3` |
| `business-ba005` | `2` | `paid_search, 3` |
| `business-ba006` | `4` | `Field Event, 2` |
| `business-ba010` | `1` | `Northstar Health, 2024-03-25` |
| `business-ba011` | `2` | `SaaS, 8.0` |
| `business-ba012` | `1` | `Vector Retail, smb, 24000.0` |
| `business-ba014` | `1` | `Northstar Health, Ada Cruz` |
| `business-ba017` | `1` | `Omar Diaz, 2` |
| `business-ba019` | `4` | `Partner Push, 100.0` |
| `business-ba020` | `2` | `mid_market, 50.0` |
| `platform-pq004` | `1` | `Jon Bell, 2` |
| `platform-pq005` | `1` | `Jon Bell, 9.5` |

## Interpretation

This proves the local application boundary for the current semantic-alias gap:
the runtime can fail closed, then a typed proposal can be locally validated and
selected as a fallback SQL candidate without using direct LLM SQL.

This is still not proof that a live provider can independently create the typed
proposals. That remains the next external smoke once provider credentials are
available in the shell.
