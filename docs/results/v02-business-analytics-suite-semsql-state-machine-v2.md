# Business Analytics Suite: SemSQL State-Machine v2

Date: 2026-06-02. Historical state-machine snapshot; use
[v02-pathway-benchmark-bound-plan-v30.md](v02-pathway-benchmark-bound-plan-v30.md)
and [v02-evidence-ledger.md](v02-evidence-ledger.md) as current evidence.

## Result

| Check | Result | Meaning |
|---|---:|---|
| Route targets | `20/20` | governed BI/CRM/growth/sales/ops slice passed |
| Clarify/reject probes | `6/6` | ambiguous, unsafe, write, and causal prompts failed closed |
| Accepted route stage | `20/20 stage_0a` | no Stage 3 guessing on this suite |
| Errors/timeouts | `0/0` | runtime stable on the fixture |

## Generic Work Preserved

The slice added schema-role detection, grouped sum/count/average frames,
top-k open-pipeline routing, month/quarter date normalization, owner/display
projections, support-renewal intersection, conditional-ratio metrics,
start/end-date duration metrics, governed temporal anti-joins, and fail-closed
guards for unsupported scalar ratios.

Covered route families: renewals, grouped metrics, CRM pipeline, growth funnel,
customer success, support ops, chart-friendly daily counts, domain lookup,
ratio metrics, and duration metrics.

## Boundaries

This was not a full BI semantic layer. Undefined metrics, unsafe PII exports,
write-side effects, causal analysis, unsupported absence/subquery variants, and
unimplemented metric dimensions must still clarify or reject.

Verification included runtime tests
`runtime_query_frame_routes_business_analytics_shapes` and
`runtime_query_frame_fails_closed_for_unsupported_business_analytics_shapes`.
