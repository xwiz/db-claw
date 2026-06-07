# BoundQueryPlan Issue #3 Audit v1

Status: retained issue #3 audit evidence. Live status lives in
`v02-current-status.md`; benchmark acceptance for #1 remains open.

Date: 2026-06-07.

## Scope

Issue #3 asked for the product path to stop treating Stage 3/4 as a free-form
SQL escape hatch. The current product gate now requires accepted SQL to be
backed by a valid `BoundQueryPlan` packet, while older Stage 3/4 repair behavior
is kept only as diagnostic/backward-compatibility debt.

Code anchors:

- `crates/semsql-runtime/src/lib.rs`: `BoundQueryPlanTrace`,
  `runtime_bound_query_plan_from_trace`, and product SQL emission from
  `bound_query_plan.sql`.
- `python/semsql_eval/src/semsql_eval/pathway_benchmark.py`:
  `PRODUCT_GATE_ZERO_SIGNALS` blocks promoted or Stage 3 SQL if any wrong-SQL
  signal goes positive.

## Verification

```powershell
uv run --package semsql-eval python -m semsql_eval pathway-benchmark `
  --out target/v02/issue3-boundary-gate-v1/pathway `
  --semsql-bin target/debug/semsql.exe `
  --schema-variant semantic_alias `
  --out-json target/v02/issue3-boundary-gate-v1/pathway.json `
  --out-md target/v02/issue3-boundary-gate-v1/pathway.md `
  --strict
```

Fresh strict run:

- cases: `44`
- route cases: `31`
- non-route cases: `13`
- `frame_only`: route correct `31/31`, wrong SQL `0`, non-route unexpected SQL
  `0`
- `bound_plan`: route correct `31/31`, wrong SQL `0`, non-route unexpected SQL
  `0`
- `stage3_sql_total = 0`
- `stage3_route_wrong_sql = 0`
- `stage3_nonroute_unexpected_sql = 0`
- `stage3_sql_with_escalation = 0`
- `frame_promoted_route_wrong_sql = 0`
- `bound_plan_route_wrong_sql = 0`
- `bound_plan_nonroute_unexpected_sql = 0`

## Read

Close issue #3 after CI confirms this audit change. Keep issue #1 open:
this proves the product boundary on the practical pathway suite, not arbitrary
BIRD/Spider benchmark readiness.
