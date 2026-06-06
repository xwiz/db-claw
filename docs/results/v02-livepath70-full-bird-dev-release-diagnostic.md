# v0.2 Livepath70 Full BIRD Dev Release Diagnostic

Date: 2026-05-30

> Historical diagnostic only: this report predates the 2026-06-02 static
> shortcut cleanup. Do not use it as release evidence or current benchmark
> proof. The cleaned runtime needs a fresh full BIRD dev rerun.

## Scope

Full BIRD dev release run after the `livepath67` card timeout fixes and the
`livepath69` codebase location-count timeout fix.

Retained report:
`v02-livepath70-full-bird-dev-release-q20-report.json`

Gate report:
`v02-livepath70-full-bird-dev-release-gate-report.md`

## Result

| metric | value |
|---|---:|
| correct | `726/1534 = 47.33%` |
| wrong | `808/1534` |
| bails | `0` |
| errors | `0` |
| timeout buckets | `0` |
| subprocess timeouts | `0` |
| gold exec timeouts | `3` |
| stage breakdown | `stage_0a=483`, `stage_3=1051` |
| average query wall time | `5.81s` |
| p95 query wall time | `10.96s` |

At the time of this historical run,
`python -m semsql_eval gate-report --profile v0.2-bird` passed:
`exec_acc=47.327%`, `errored=0`, `timeouts=0`.

## Comparison

| run | correct | timeout buckets | read |
|---|---:|---:|---|
| `livepath62` full live baseline | `668/1534 = 43.55%` | `46` | previous full-dev baseline |
| `livepath68` full release diagnostic | `725/1534 = 47.26%` | `1` | exposed one codebase subprocess timeout at index `625` |
| `livepath70` full release gate | `726/1534 = 47.33%` | `0` | historical promoted full-dev result |

## By DB

| DB | correct / total | accuracy | read |
|---|---:|---:|---|
| `california_schools` | `88/89` | `98.88%` | solved sentinel |
| `european_football_2` | `91/129` | `70.54%` | largest lift versus `livepath62` |
| `formula_1` | `94/174` | `54.02%` | improved versus `livepath62` |
| `debit_card_specializing` | `29/64` | `45.31%` | unchanged |
| `thrombosis_prediction` | `74/163` | `45.40%` | unchanged |
| `codebase_community` | `83/186` | `44.62%` | one timeout fixed; still broad exec mismatch gap |
| `card_games` | `80/191` | `41.88%` | card timeout bucket fixed; still broad exec mismatch gap |
| `toxicology` | `58/145` | `40.00%` | unchanged |
| `student_club` | `61/158` | `38.61%` | slight lift |
| `financial` | `31/106` | `29.25%` | unchanged and still weak |
| `superhero` | `37/129` | `28.68%` | slight lift but still weakest large family |

## Read

At the time, this run made the v0.2 benchmark gate credible on runtime
criteria: full BIRD dev had `0` bails, `0` errors, and `0` timeout buckets
under the standard q20/q5 release path. That claim is now historical only after
the static-shortcut cleanup.

The next work then would have moved from runtime cleanup to accuracy. The
highest-value families were `superhero`, `financial`, `student_club`,
`toxicology`, `card_games`, and `codebase_community`. The historical failure
shape was mostly `exec_mismatch`, not pipeline instability.
