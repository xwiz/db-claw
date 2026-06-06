# v0.2 Evidence Ledger
Date: 2026-06-06. Anchors only; status lives in [v02-current-status.md](v02-current-status.md).

| Area | Anchor | Reports |
|---|---:|---|
| Core | `release_candidate`; CI/release `31/31`; canary `144/144 + 18/18`; runtime `59/59`; wrong SQL `0` | [readiness](v02-production-readiness-release-candidate-v1.md), [core](v02-pathway-benchmark-bound-plan-v30.md), [para](v02-pathway-benchmark-paraphrase-v5.md), [alias](v02-pathway-benchmark-random-alias-breadth-v1.md) |
| Fallback | fail-closed; provider `6/6 + 3/3`; safety `6/6 + 4/4`; shapes pass; multi-series `15/15` | [safety](v02-llm-resolution-safety-boundary-v1.md), [mixed](v02-llm-resolution-mixed-safety-v1.md), [live](v02-live-provider-openai-batch-v1.md), [shape](v02-provider-result-shape-contract-v1.md), [multi](v02-realdb-typed-fallback-result-shape-multiseries-v1.md) |
| Real DBs | MariaDB `53/53`, broader `60/60`, BI/ops `46/46`; Postgres `6/6 + 3/3`, fallback `4/4` | [MariaDB](v02-realdb-typed-fallback-mariadb-suite-refresh-v20.md), [broader](v02-realdb-broader-mariadb-probes-v2.md), [PG](v02-realdb-schema-probe-postgres-disposable-v1.md), [PG fallback](v02-realdb-typed-fallback-postgres-disposable-v1.md) |
| Frameworks | bridge `5/5`; Laravel `4962/4962`, `13/13`, `6/6`; Next.js `174/174`, `5/5`; release package path `9/9 + 6/6` | [framework](v02-framework-extract-bridge-probe-v2.md), [Laravel](v02-real-app-framework-mailer-web-v2.md), [fraudv](v02-real-app-framework-fraudv-v1.md), [Next](v02-real-app-framework-hostshell-nextjs-v3.md), [pkg](v02-public-package-smoke-alpha5-portable-v1.md) |
| Research | focused `13/13`; BIRD first-100 `5/100`; sharded ambiguity fails closed | [focused](v02-focused13-full-recovery-slice.md), [BIRD](v02-bird-first100-after-focused13.md), [shard](v02-realdb-mailer-web-sharding-audit-cleaned-v2.md) |

Promote private alpha only with `0` wrong accepted SQL and fail-closed non-routes; treat BIRD and pre-cleanup v10-v17 as research only.
