# v0.2 BIRD SemanticAtlas Direction
Date: 2026-06-09. Retained benchmark decision; current numbers live in [the ledger](v02-evidence-ledger.md).

## Decision
Treat BIRD failures as DB-only SemanticAtlas coverage/planning failures, not permission to add benchmark-shaped tables, static examples, or direct SQL generation:

`database -> DB-only atlas -> typed intent -> candidate plans -> guarded SQL`

## Rules
- Build the same virtual atlas a customer DB receives: schema, relationships, types, activity hints, bounded non-PII values, roles, metrics, dates, provenance, and confidence.
- Do not use dev gold SQL, per-question examples, or BIRD-specific maps.
- Similarity generates candidates only; it cannot invent a metric, relationship, value binding, or SQL shape.
- Render only complete validated plans. Otherwise emit `ask_user`, `ask_llm`, or `reject`.

## Retained Findings
- Description-aware first50 remained `3/50`; targeted reusable fixes reached `7/7`, wrong `0`.
- First20 is `7/20`, wrong `0`, bailed `13`; ambiguous duplicate values now fail closed and packetize field-scoped candidates.
- Remaining failures concentrate in metric/ranking/group/order binding and weak DB-only semantics, not a lack of query-specific aliases.

Keep BIRD as raw-DB stress research while application-aware accuracy drives release work.
