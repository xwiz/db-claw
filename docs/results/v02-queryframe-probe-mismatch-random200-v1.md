# QueryFrame Solver Probe

- sample_size: `115`
- proof_ready_only: `True`
- routed_only: `False`

## Summary

| metric | value |
|---|---:|
| `proof_ready` | `115` |
| `routed` | `50` |
| `routed_coverage` | `43.48%` |
| `correct` | `50` |
| `routed_exec_acc` | `100.00%` |
| `net_recovery` | `50` |
| `regressions_from_current` | `0` |
| `pred_errors` | `0` |
| `pred_timeouts` | `0` |
| `gold_timeouts` | `0` |

## Route Buckets

| bucket | n |
|---|---:|
| `correct` | 50 |
| `not_routed_complex_shape` | 31 |
| `not_routed_no_predicates` | 12 |
| `not_routed_multi_projection` | 8 |
| `not_routed_unsafe_id_projection` | 6 |
| `not_routed_unbound_year` | 5 |
| `not_routed_too_many_predicates` | 2 |
| `not_routed_no_projection` | 1 |

## By DB

| DB | routed | correct | exec acc |
|---|---:|---:|---:|
| `california_schools` | 0 | 0 | 0.00% |
| `card_games` | 11 | 11 | 100.00% |
| `codebase_community` | 5 | 5 | 100.00% |
| `debit_card_specializing` | 0 | 0 | 0.00% |
| `european_football_2` | 1 | 1 | 100.00% |
| `financial` | 4 | 4 | 100.00% |
| `formula_1` | 2 | 2 | 100.00% |
| `student_club` | 12 | 12 | 100.00% |
| `superhero` | 11 | 11 | 100.00% |
| `thrombosis_prediction` | 0 | 0 | 0.00% |
| `toxicology` | 4 | 4 | 100.00% |

## Read

This is an experimental SQL execution probe for the QueryFrame path.
It routes only examples the deterministic solver can build from the
question text, schema/value atlas, and binder evidence. Gold SQL is
used only for execution-equivalence scoring.
