# v0.2 Evidence Ledger
Date: 2026-06-10. Anchors only; status: [current](v02-current-status.md).

| Area | Anchor | Reports |
|---|---:|---|
| Core | guarded reads; wrong SQL `0`; four-way decision; correction improves rerun | [#3](v02-boundqueryplan-issue3-audit-v1.md), [atlas](v02-virtual-semantic-atlas-tables-v1.md), [gate](v02-atlas-backed-route-promotion-v1.md) |
| Fallback | fail-closed; typed safety/shape/multi pass; direct SQL rejected | [safety](v02-llm-resolution-safety-boundary-v1.md), [live](v02-live-provider-openai-batch-v1.md), [shape](v02-provider-result-shape-contract-v1.md) |
| Real DBs | generated probes: MariaDB `53/53`, broader `60/60`, BI/ops `46/46`; PG `6/6 + 3/3`, fallback `4/4` | [MariaDB](v02-realdb-typed-fallback-mariadb-suite-refresh-v20.md), [PG](v02-realdb-schema-probe-postgres-disposable-v1.md) |
| Frameworks | Laravel source join + correction loop; fraudv, Next, extractor bridge | [alpha](v02-laravel-private-alpha-probe-v1.md), [bridge](v02-framework-extract-bridge-probe-v2.md), [real app](v02-real-app-framework-mailer-web-v2.md) |
| Benchmarks | BIRD100 `3/100`; first20 `7/20`, wrong `0`; targeted `7/7` | [BIRD100](v02-bird100-onnx-diagnostic-v1.md), [direction](v02-bird-semantic-atlas-direction-v1.md), [focused](v02-focused13-full-recovery-slice.md) |

Promote only with `0` wrong accepted SQL, fail-closed non-routes, and actionable handoffs.
