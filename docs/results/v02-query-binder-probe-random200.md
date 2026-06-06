# Query Binder Atlas Proof: All-Dev Random Probe

- seed: `20260530`
- sample_size: `200`
- population_size: `1534`
- only_mismatches: `False`

## Summary

| metric | value |
|---|---:|
| `current_exec_acc_on_sample` | `49.50%` |
| `parse_ok_rate` | `100.00%` |
| `table_recall_avg` | `78.83%` |
| `field_recall_avg` | `49.14%` |
| `literal_mention_recall_avg` | `73.15%` |
| `value_field_recall_avg` | `77.17%` |
| `join_path_hit_rate` | `84.43%` |
| `aggregate_hit_rate` | `66.38%` |
| `proof_ready_rate` | `47.00%` |

## By DB

| DB | n | proof-ready | value-field recall | table recall |
|---|---:|---:|---:|---:|
| `california_schools` | 14 | 50.00% | 63.10% | 75.00% |
| `card_games` | 25 | 64.00% | 86.67% | 98.00% |
| `codebase_community` | 22 | 50.00% | 75.76% | 91.67% |
| `debit_card_specializing` | 6 | 16.67% | 50.00% | 44.44% |
| `european_football_2` | 14 | 28.57% | 82.14% | 96.43% |
| `financial` | 14 | 50.00% | 53.57% | 73.81% |
| `formula_1` | 23 | 56.52% | 81.16% | 77.90% |
| `student_club` | 28 | 57.14% | 76.79% | 71.43% |
| `superhero` | 17 | 64.71% | 76.47% | 80.88% |
| `thrombosis_prediction` | 22 | 13.64% | 77.27% | 69.70% |
| `toxicology` | 15 | 33.33% | 100.00% | 60.00% |

## Read

This is a pre-generation proof, not a SQL execution benchmark. It scores whether
a database atlas can recover the evidence needed by a deterministic QueryFrame
solver: tables, fields, literal-to-field bindings, aggregate cues, and join
reachability. Gold SQL is used only after binding to score the probe.

A high value-field and join-path rate means the database already contains enough
signal to bind noun/value mentions before SQL generation. Low proof-ready cases
identify where we need synonym lexicons, derived operators, or better schema
role hints before investing in the full compiler path.
