# v0.2 Production Readiness Release Candidate

Date: 2026-06-06. Retained evidence report; current decision lives in
[v02-current-status.md](v02-current-status.md).

Strict production readiness now passes for the pre-release/private-alpha gate.
This is not a broad arbitrary NL-to-SQL benchmark claim.

| Surface | Result |
|---|---:|
| Pathway | pass |
| QueryFrame canary | pass |
| LLM safety | pass |
| real DB probes | pass |
| framework probes | pass |
| public package smoke | pass |

Summary: `release_candidate=True`, `pilot_safe=True`, wrong accepted SQL `0`,
fail-closed route gaps `0`.

Key evidence:
- MariaDB dynamic suite: `53/53`, including rate/grouped/value/joined analytics families.
- Postgres disposable fallback probe: pass.
- Framework bridge plus real Next.js probe: pass.
- GitHub release run `27074347744`: public package smoke pass.

Artifacts: `target/v02/production-readiness-current/`.
