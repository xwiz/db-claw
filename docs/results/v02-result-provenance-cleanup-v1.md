# v0.2 Result Provenance Cleanup
Date: 2026-06-07. Retained cleanup note; current status remains in [v02-current-status.md](v02-current-status.md).

## Implemented

- BIRD/Spider JSON reports now include `metadata.provenance` with run start time, report write time, `semsql_eval` version, and `semsql --version` output.
- Top-level CLI JSON result reports now include `provenance.generated_at_utc` and `provenance.semsql_eval_version`; protocol-shaped packets, request previews, schema cards, and generated suite fixtures are left unchanged.
- Production-readiness JSON/Markdown reports now include generation provenance and exact input report paths.
- Docs hygiene now supports `--fail-missing-provenance-for-changed`, requiring new or edited `docs/results/*.md` reports to carry a top-of-file date stamp or exact package/version.

## Rerun 2026-06-07

| Surface | Result | Artifact |
|---|---:|---|
| `target/debug/semsql.exe --version` | `semsql 0.1.0-alpha.5` | terminal |
| docs hygiene plus provenance guard | pass | terminal |
| git artifact guard | pass, `543` visible files | terminal |
| QueryFrame canary suite | pass, `144/144` routed and `18/18` fail-closed | `target/v02/rerun-20260607/queryframe-canary-suite/report.json` |
| Pathway benchmark | pass, `31/31` routes and `13/13` fail-closed non-routes | `target/v02/rerun-20260607/pathway-benchmark/pathway_benchmark.json` |
| core readiness index | `pilot_safe=True`, `release_candidate=False`, wrong SQL `0`, route gaps `0` | `target/v02/rerun-20260607/production-readiness-core/report.json` |

## Not Rerun Here

These remain release-surface reruns, not local deterministic reruns:

- LLM/live-provider safety: requires provider credentials and model choice.
- Real DB MariaDB/Postgres suites: require current private DB URLs and read-only users.
- Framework real-app probes: require current private app paths and DB URLs.
- Public package smoke: requires published package/manifest network state.
- BIRD benchmark: no current BIRD result is claimed; rerun only after deciding the current benchmark/training scope.

## Cleanup Notes

- `v02-current-status.md` no longer cites the stale June 2 BIRD `5/100` diagnostic.
- `v02-evidence-ledger.md` labels that BIRD artifact as historical research.
- Ignored local JSON under `artifacts/results-json/docs-results` was quarantined to `artifacts/results-json/archive-20260607/docs-results` to keep stale June 2/June 4 artifacts out of the current-looking docs-results cache.
- Legacy retained reports without top-of-file provenance are tolerated only as archive history; any touched or new retained report must be stamped before hygiene passes.
