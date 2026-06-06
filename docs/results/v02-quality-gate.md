# v0.2 Quality Gate, 2026-06-06

Release checklist only. Numbers live in [v02-evidence-ledger.md](v02-evidence-ledger.md); aggregate with `python -m semsql_eval production-readiness-report`.

## Required Checks

| Area | Gate | Status |
|---|---|---:|
| Code | Rust fmt/clippy/tests, Python ruff/mypy/pytest, pnpm lint/test/typecheck/build | pass |
| Runtime | atlas/queryframe tests, static shortcut audit, artifact guard | pass |
| Product | CI/release pathway/queryframe gate, aliases, fallback, real DB/framework probes, sharding ambiguity | pass |
| Packaging | alpha rehearsal, launcher/local registry/dlx, package version/runtime-literal/metadata/scope checks | pass |
| Release | clean non-dev tag preflight, real workflow, public package smoke without local binary override | binary/npm pass; CI public smoke pending rerun |

Run `pnpm -r typecheck` before `pnpm -r build`; build scripts clean `dist`.
`v0.1.0-alpha.5` passed the GitHub binary release workflow and npm publication.
Npm publication is manual via `workflow_dispatch publish_npm=true`; tag pushes should not publish packages by accident.
`v0.1.0-dev` must fail as JSON, not a traceback.
Diagnostics must expose evidence, fields/values/joins/metrics/date anchors, fail-closed reason, fallback packet, and result shape.

## Stop Release If

- accepted wrong SQL increases;
- adds a static/query-specific runtime shortcut;
- accepts direct provider SQL or unsupported partial plans;
- weakens read-only execution, row-discarding, or artifact policy;
- resolves ambiguous sharded tables without evidence.

Benchmark note: BIRD is research only until cleaned-runtime coverage improves; pre-cleanup v10-v17 reports are historical.
