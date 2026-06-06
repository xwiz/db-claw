# v0.2 Multi-Projection QueryFrame Slice

Date: 2026-06-02. Historical slice; current status lives in
[v02-current-status.md](v02-current-status.md).

## Purpose

Added generic multi-projection lookup support for prompts shaped like
`show/list <field A> and <field B> for <grounded predicate>`. This is not a
BIRD-specific route.

## Preserved Contract

- `RuntimeQueryFrameProjectionTrace.fields` reports every selected projection.
- Rendering may emit multiple safe display fields.
- Join planning includes every projected entity, not just the primary one.
- ID-like fields are role-gated so IDs can be predicates or projections only
  when the wording supports that role.
- Commas inside quoted literals do not create fake projection lists.

## Result

| Signal | Value |
|---|---:|
| Product canary | `PASS`, `9/9` runs |
| Routed exec accuracy | `144/144` |
| Reject fail-closed | `18/18` |
| Focused BIRD trace | `3/13` correct, `7` wrong, `3` bails |
| BIRD first-20 smoke | `0/20`, `10` wrong, `9` bails, `1` structural error |

The remaining `financial` failure was due to opaque `A2`/`A3` fields without
description ingestion. That was addressed later by
`v02-schema-description-slice.md`.

## Next From This Slice

Schema/description vocabulary and MetricAtlas enrichment were required next;
multi-projection alone was working only for well-labeled schemas.
