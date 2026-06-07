# v0.2 Evidence Ledger
Date: 2026-06-07. Anchors only; status lives in [v02-current-status.md](v02-current-status.md).

| Area | Anchor | Reports |
|---|---:|---|
| Core | `pilot_safe`; wrong SQL `0`; gaps `0`; #3 signals `0`; virtual atlas and text-value gate added | [rerun](v02-result-provenance-cleanup-v1.md), [#3](v02-boundqueryplan-issue3-audit-v1.md), [atlas](v02-virtual-semantic-atlas-tables-v1.md), [gate](v02-atlas-backed-route-promotion-v1.md), [ready](v02-production-readiness-release-candidate-v1.md), [core](v02-pathway-benchmark-bound-plan-v30.md) |
| Fallback | fail-closed; safety/shape/multi pass; family/activity hints surfaced | [safety](v02-llm-resolution-safety-boundary-v1.md), [mixed](v02-llm-resolution-mixed-safety-v1.md), [live](v02-live-provider-openai-batch-v1.md), [shape](v02-provider-result-shape-contract-v1.md), [multi](v02-realdb-typed-fallback-result-shape-multiseries-v1.md) |
| Real DBs | MariaDB `53/53`, broader `60/60`, BI/ops `46/46`; PG `6/6 + 3/3`, fallback `4/4` | [MariaDB](v02-realdb-typed-fallback-mariadb-suite-refresh-v20.md), [broader](v02-realdb-broader-mariadb-probes-v2.md), [PG](v02-realdb-schema-probe-postgres-disposable-v1.md), [fallback](v02-realdb-typed-fallback-postgres-disposable-v1.md) |
| Frameworks | bridge, Laravel, fraudv, Next, package, Sheets | [framework](v02-framework-extract-bridge-probe-v2.md), [Laravel](v02-real-app-framework-mailer-web-v2.md), [fraudv](v02-real-app-framework-fraudv-v1.md), [Next](v02-real-app-framework-hostshell-nextjs-v3.md), [pkg](v02-public-package-smoke-alpha5-portable-v1.md), [sheets](v02-sheets-demo-pages-v1.md) |
| Benchmarks/research | BIRD100 ONNX `3/100`, route-used wrong `26/29`; atlas-gated partial `50/100` has route-used wrong `6`; focused `13/13`; shard ambiguity fail-closed | [BIRD100](v02-bird100-onnx-diagnostic-v1.md), [atlas gate](v02-atlas-backed-route-promotion-v1.md), [focused](v02-focused13-full-recovery-slice.md), [shard](v02-realdb-mailer-web-sharding-audit-cleaned-v2.md) |

Promote private alpha only with `0` wrong accepted SQL and fail-closed non-routes; treat BIRD and pre-cleanup v10-v17 as research until #1 closes.
