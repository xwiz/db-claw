# v0.2 Stage 3 And Visualization Assessment

Date: 2026-06-05

Retained decision memo. Current gate numbers live in
`v02-evidence-ledger.md`.

## Decision

Stage 3 is useful, but only as a bounded ranker over legal candidates. It must
not repair rejected plans, invent final SQL, or bypass `BoundQueryPlan`.

Result visualization is a separate typed-output problem. Add result-shape hints
to accepted plans; do not couple chart selection to SQL generation.

## Evidence To Preserve

| Signal | Read |
|---|---|
| Broad Stage 3 fallback, `livepath70` | `283/1051 = 26.93%`; weak when asked to plan globally |
| Historical narrowed v17 slice | `263/267 = 98.50%`; high precision because fewer cases reached Stage 3 |
| Product pathway v30 | `31/31`, `0` wrong accepted SQL through governed frames |
| Semantic-alias pressure | platform `11/11`, BI `14/20`, `0` wrong accepted SQL |
| LLM typed fallback smoke | recovered current semantic-alias false negatives as typed proposals, not provider SQL |

The lesson is not "train Stage 3 harder." It is "build a legal candidate set
first, then rank only inside that set."

## Keep

- Stage 3 or small rankers for field/value/join choice when legal candidates
  are already present.
- Fail-closed behavior when the frame is missing a required field, value, join,
  metric, date role, or safety scope.
- Typed fallback packets for rejected cases.

## Do Not Do

- Do not let Stage 3 emit SQL after graph routing rejects a query.
- Do not treat historical v10-v17 BIRD recovery as current release evidence.
- Do not add static app/example phrase maps to make complex examples pass.
- Do not use provider SQL directly.

## Visualization Boundary

Attach a `result_shape` to validated plans:

| Shape | Output hint |
|---|---|
| one aggregate, no group | scalar metric |
| categorical group plus aggregate | bar/pie-capable dataset |
| time bucket plus aggregate | time series |
| two dimensions plus aggregate | multi-series candidate |
| projection/list | table |

The renderer should return SQL plus shape metadata. Chart.js/table adapters can
consume that metadata later without changing routing correctness.

## Next

1. Keep improving `SemanticAtlas -> IntentFrame -> BoundQueryPlan`.
2. Expand BI/customer analytics metric and date primitives.
3. Use typed fallback for unresolved long-tail planning.
4. Promote only when wrong accepted SQL stays `0`.
