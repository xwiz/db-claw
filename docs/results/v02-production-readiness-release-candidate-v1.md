# v0.2 Historical Private-Alpha Readiness Probe

Date: 2026-06-06. Retained evidence report; current decision lives in
[v02-current-status.md](v02-current-status.md).

Historical probe only. Do not use this file as the current product-readiness
decision. The active release gate now requires the resolution-decision loop:
`execute`, `ask_user`, `ask_llm`, or `reject`, plus durable approved mappings.
This was never a broad arbitrary NL-to-SQL benchmark claim.

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
