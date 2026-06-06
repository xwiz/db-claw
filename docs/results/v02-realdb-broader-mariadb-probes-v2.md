# v0.2 Broader MariaDB Probes v2
Date: 2026-06-05

Purpose: prove the current SemanticAtlas/typed-plan path on more real schemas
without turning the status dashboard into a run log.

## Result

Pass. Six additional MariaDB schemas passed schema-only safety probing, and four
of those passed the broader typed-fallback BI/ops probe after the generic
grouped-aggregate fixes.

## Schema-Only Probe

Safety mode: schema-only extraction, count-only execution, governed analytics
execution, no result values retained.

| Database | Questions | Status | Replay |
|---|---:|---|---|
| `cloudspace_kyc_dashboard` | 12 | pass | `target/v02/realdb-schema-probe-mariadb-broaden-v1/cloudspace_kyc_dashboard` |
| `el_biblio` | 12 | pass | `target/v02/realdb-schema-probe-mariadb-broaden-v2/el_biblio` |
| `fraudv_go` | 10 | pass | `target/v02/realdb-schema-probe-mariadb-broaden-v1/fraudv_go` |
| `guardrail` | 11 | pass | `target/v02/realdb-schema-probe-mariadb-broaden-v1/guardrail` |
| `hostshell` | 10 | pass | `target/v02/realdb-schema-probe-mariadb-broaden-v1/hostshell` |
| `maildb` | 5 | pass | `target/v02/realdb-schema-probe-mariadb-broaden-v1/maildb` |

Total: `60/60` semantic-ok or safe-not-executed under the probe rules.

## Typed-Fallback BI/Ops Probe

Safety mode: schema-only extraction plus bounded non-PII sample values; provider
set to `none`; typed providers may propose plans only; SQL is locally rendered,
validated, executed read-only, and rows are discarded.

Families: conditional rate, grouped average, filtered grouped average,
value-filtered grouped average, joined filtered grouped average, and multi-hop
joined filtered grouped average.

| Database | Questions | Selected | Executed | Expected Match | Provider Calls | Rows Retained | Replay |
|---|---:|---:|---:|---:|---:|---:|---|
| `el_biblio` | 12 | 12 | 12 | 12 | 0 | 0 | `target/v02/realdb-typed-fallback-mariadb-broaden-v2/el_biblio` |
| `fraudv_go` | 12 | 12 | 12 | 12 | 0 | 0 | `target/v02/realdb-typed-fallback-mariadb-broaden-v2/fraudv_go` |
| `guardrail` | 12 | 12 | 12 | 12 | 0 | 0 | `target/v02/realdb-typed-fallback-mariadb-broaden-v2/guardrail` |
| `hostshell` | 10 | 10 | 10 | 10 | 0 | 0 | `target/v02/realdb-typed-fallback-mariadb-broaden-v2/hostshell` |

Total: `46/46` selected, executed, and expected matched; `0` provider calls;
`0` rows retained.

## Regression Lessons Locked

- Explicit related-entity dimensions must win when the prompt names them:
  examples include `agents model`, `bank accounts currency`, and `themes color
  code`.
- Aggregate measures must be scoped by explicit subjects such as `for sl
  configs` before accepting a foreign exact phrase like `average_price`.
- Measure phrases must not become equality predicates unless the literal value
  is explicitly requested.
- Quoted one-character categorical values such as `'F'` are valid value
  evidence and must replace weaker same-field guesses.
- Temporal words inside measure names such as `current daily sum` or `current
  weekly count` must not force a time-series route when the prompt is top-k
  grouped aggregate.

## Limits

This is production-path evidence for governed read-only BI/ops shapes, not a
claim that arbitrary natural-language SQL, direct provider SQL, sharded-table
resolution, or broad BIRD accuracy is solved.
