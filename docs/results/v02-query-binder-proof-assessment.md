# v0.2 Query Binder Proof Assessment

Date: 2026-05-30

Status: historical proof for the QueryFrame/atlas path. Current product gates
live in `v02-evidence-ledger.md`.

## Question

Before investing in a state-machine compiler, could a small schema/value atlas
recover enough evidence from random examples to make the approach credible?

## Signal

| seeded slice | current exec acc | value-field recall | join-path hit | aggregate hit | proof-ready |
|---|---:|---:|---:|---:|---:|
| BIRD dev random `n=200` | `49.50%` | `77.17%` | `84.43%` | `66.38%` | `47.00%` |
| current mismatches `n=200` | `0.00%` | `82.30%` | `87.69%` | `69.03%` | `57.50%` |

Follow-up execution proof on the frozen mismatch slice routed `50` proof-ready
examples at `50/50` execution accuracy with zero runtime failures.

## Decision Kept

Proceed with a narrow typed QueryFrame/BoundQueryPlan path:

```text
NL -> typed mentions -> schema/value atlas -> frame solver -> deterministic SQL
```

This does not prove arbitrary NL-to-SQL is solved. It proves many failures have
recoverable evidence and should be attacked with a planner before adding model
capacity.

## Retained Detail

- `v02-query-binder-probe-random200.json`
- `v02-query-binder-probe-mismatch-random200.json`
- `v02-queryframe-probe-mismatch-random200-v1.json`

## Regression Rule

Use seeded mismatch samples for proof, not cherry-picked queries. Accept SQL
only for evidence-complete plans; otherwise fail closed or emit typed fallback
packets.
