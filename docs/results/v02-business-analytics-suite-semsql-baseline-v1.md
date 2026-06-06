# Business Analytics Suite SemSQL Baseline v1

Date: 2026-06-02

Status: historical BI baseline. Superseded by
`v02-business-analytics-suite-semsql-state-machine-v2.md` and current pathway
evidence in `v02-evidence-ledger.md`.

## Signal

| check | result |
|---|---:|
| route targets | `1/17` |
| clarify/reject probes | `6/6` fail closed |
| errors/timeouts | `0/0` |
| only route pass | `ba013` structured domain lookup |

Raw route report:
`target/business_analytics_suite_v1/semsql-business-route-report-release-after-guard.json`.

## Lessons Kept

- Exact structured literal lookup worked early; practical BI planning did not.
- Needed governed frames for grouped metrics, top-k metrics, multi-projection
  lists, time windows, two-fact intersections, and derived formulas.
- Needed a semantic catalog for ARR, MRR, pipeline, renewal, churn, overdue,
  NPS, sales cycle, and similar business metrics.
- Unsafe exports, broad dumps, causal analysis, and undefined metrics should
  fail closed before Stage 3.

## Superseded Result

The later state-machine path moved this suite to `20/20` route targets while
preserving non-route fail-closed behavior. Use current evidence links from
`docs/results/README.md`.
