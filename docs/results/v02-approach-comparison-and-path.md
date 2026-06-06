# v0.2 Approach Comparison And Best Path

Date: 2026-06-05

Historical design memo. Current status and numbers live in
`v02-current-status.md` and `v02-evidence-ledger.md`.

## Decision

Do not continue by broadly retraining Stage 2/3 or adding isolated static
frames.

Proceed with a source-grounded `SemanticAtlas -> IntentFrame -> BoundQueryPlan`
path. Use models only to rank legal candidates or propose typed plans after
local rejection.

## What We Compared

| Path | What it proved | Limitation |
|---|---|---|
| MVP-era graph routing | scoped rendering and validation can work | did not prove open-domain planning, value grounding, or derived metrics |
| Stage 1/2/3 cascade | useful local ranking/fallback infrastructure | can emit legal-looking SQL with wrong fields, joins, or aggregates |
| deterministic frames | high precision for reusable shapes | brittle if they encode DB-family facts |
| schema/value binder | graph often contains enough evidence | proof-ready is not execution accuracy |

## Historical Signals

- Pre-cleanup recovered BIRD runs reached high numbers through a mix of useful
  generic work and static/domain shortcut risk. They are not current release
  evidence.
- Binder probes found proof-ready evidence in `47.00%` of random BIRD dev and
  `57.50%` of livepath70 mismatches.
- The cleaned product line now values `0` wrong accepted SQL over broad but
  unsafe benchmark recovery.

## Current Runtime Shape

```text
question
  -> typed mentions and values
  -> schema/value/metric candidate retrieval
  -> IntentFrame
  -> BoundQueryPlan
  -> local SQL rendering
  -> validation and optional read-only execution
```

## Stop Rules

- Do not treat proof-ready as execution accuracy.
- Do not use pre-cleanup v10-v17 reports as readiness proof.
- Do not add one-off frames unless they generalize into reusable transitions.
- Do not scan arbitrary text columns at query time.
- Do not route through legacy/domain specialists.
- Do not train broad SQL generation unless traces show legal candidates are
  present and only misranked.
