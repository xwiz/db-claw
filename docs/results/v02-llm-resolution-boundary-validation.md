# v0.2 LLM Resolution Boundary Validation

Date: 2026-06-03

Status: historical architecture validation. Current runtime gates live in
`v02-evidence-ledger.md`.

## Decision Kept

LLM fallback is useful only as typed plan proposal over bounded evidence:

```text
fail-closed packet -> typed proposal JSON -> local validation -> local SQL
```

Direct provider SQL, partial typed plans, or unsupported renderer output are not
release paths.

## Historical Signal

The early prototype did not improve runtime accuracy. Practical suites were
still `3/30` route-correct with `16` wrong accepted SQL because promoted-frame
legality was the real blocker.

Oracle typed-plan probing showed the packet/renderer shape was promising but
incomplete: `18/30` render-valid and `10/30` execution-equal across route cases.

## Gaps Identified

- field-scoped value evidence;
- date/datetime field-role inference;
- aggregate aliases and top-k ordering;
- `COUNT(field)`, `DISTINCT`, `IS NULL` / `IS NOT NULL`;
- expression metrics and conditional aggregates;
- strict render failure for unsupported or partial plans.

## Superseded By

- BoundQueryPlan runtime gate: `v02-pathway-benchmark-bound-plan-v30.md`
- Typed proposal fallback evidence:
  `v02-llm-resolution-fallback-semantic-alias-13-v1.md`

## Regression Rule

Provider assistance may choose among evidence-backed typed fields, values,
joins, metrics, dates, and result shapes. It may not bypass local validation or
render raw SQL directly.
