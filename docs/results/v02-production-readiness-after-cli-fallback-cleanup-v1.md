# v0.2 Production Readiness After CLI Fallback Cleanup

Date: 2026-06-06. Retained evidence report; current decision lives in
[v02-current-status.md](v02-current-status.md).

Removed production shortcuts for `publisher`, `charter`, fixed attribute names,
Stage 3 `school`/`cdscode` bonuses, and CLI fallback compatibility terms such
as `cds`, `charter`, and `school`. Attribute superlatives are sample-backed;
count projections use generic identifier signals.

| Surface | Result |
|---|---:|
| focused tests | CLI `52/52`; QueryFrame `59/59`; slot-bias `9/9` |
| pathway semantic-alias | `31/31` routes, `13/13` rejects, `0` wrong SQL |
| QueryFrame suite | `9/9`, `144/144` routed, `18/18` rejects |
| aggregate readiness | `pilot_safe=True`, `release_candidate=False` |

Release blocker remains public package smoke: `@semsql/*` `0.1.0-alpha.1`
packages are not visible on npm yet.

Artifacts: `target/production_readiness_after_cli_fallback_cleanup/`.
