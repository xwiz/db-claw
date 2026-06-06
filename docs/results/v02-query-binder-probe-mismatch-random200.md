# Query Binder Atlas Proof: Mismatch Random Probe

- seed: `20260530`
- sample_size: `200`
- population_size: `808`
- only_mismatches: `True`

## Summary

| metric | value |
|---|---:|
| `current_exec_acc_on_sample` | `0.00%` |
| `parse_ok_rate` | `100.00%` |
| `table_recall_avg` | `79.22%` |
| `field_recall_avg` | `49.88%` |
| `literal_mention_recall_avg` | `79.63%` |
| `value_field_recall_avg` | `82.30%` |
| `join_path_hit_rate` | `87.69%` |
| `aggregate_hit_rate` | `69.03%` |
| `proof_ready_rate` | `57.50%` |

## By DB

| DB | n | proof-ready | value-field recall | table recall |
|---|---:|---:|---:|---:|
| `california_schools` | 1 | 100.00% | 50.00% | 100.00% |
| `card_games` | 32 | 84.38% | 96.88% | 93.75% |
| `codebase_community` | 21 | 76.19% | 100.00% | 92.86% |
| `debit_card_specializing` | 7 | 42.86% | 44.29% | 64.29% |
| `european_football_2` | 8 | 37.50% | 56.25% | 75.00% |
| `financial` | 18 | 72.22% | 69.44% | 80.65% |
| `formula_1` | 21 | 38.10% | 85.71% | 67.86% |
| `student_club` | 24 | 75.00% | 79.17% | 79.86% |
| `superhero` | 23 | 73.91% | 94.20% | 92.75% |
| `thrombosis_prediction` | 23 | 17.39% | 62.32% | 70.29% |
| `toxicology` | 22 | 22.73% | 86.36% | 54.55% |

## Read

This is a pre-generation proof, not a SQL execution benchmark. It scores whether
a database atlas can recover the evidence needed by a deterministic QueryFrame
solver: tables, fields, literal-to-field bindings, aggregate cues, and join
reachability. Gold SQL is used only after binding to score the probe.

A high value-field and join-path rate means the database already contains enough
signal to bind noun/value mentions before SQL generation. Low proof-ready cases
identify where we need synonym lexicons, derived operators, or better schema
role hints before investing in the full compiler path.
