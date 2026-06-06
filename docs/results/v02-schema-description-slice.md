# v0.2 Schema Description Slice

Date: 2026-06-02

## Summary

This slice adds generic schema-description ingestion and fixes the projection
noise it exposed.

It is not a benchmark router. `semsql extract` now ingests adjacent
`database_description/*.csv` style files and uses them as field labels and
field vocabulary. This lets opaque DB columns such as `A2` / `A3` acquire
schema evidence like `district name` / `region`.

## Changes

- Added `semsql extract --schema-description-dir`.
- Auto-detects `database_description/`, `schema_description/`,
  `schema_descriptions/`, or `db_description/` under the project/DB path.
- Writes short, clean description labels into `fields.display_label`.
- Writes bounded field aliases into `vocabulary` with source layer `2`.
- Avoids treating long explanatory prose as aliases.
- Tightened QueryFrame projection scoring so stopwords from descriptions do not
  become schema evidence.
- Ordered multi-projection SELECT fields by mention order in the prompt.

## Verification

```text
cargo test -p semsql-cli db_only_extraction_ingests_column_description_csvs --features onnx
cargo test -p semsql-runtime --features onnx --lib --no-fail-fast
cargo clippy -p semsql-runtime --features onnx --lib --tests -- -D warnings
cargo clippy -p semsql-cli --features onnx -- -D warnings
cargo build -p semsql-cli --release --features semsql-cli/onnx
uv run python -m semsql_eval queryframe-canary-suite --strict ... schema-desc-v1
python scripts\check_git_artifacts.py --all
```

All passed.

Product canary:

- `v02-queryframe-canary-suite-schema-desc-v1.md`
- status `PASS`
- `9/9` runs
- `144/144` routed exec accuracy
- `18/18` reject fail-closed

Focused BIRD trace:

- report: `v02-bird-failure-trace-schema-desc-v3-report.json`
- `13` examples, `4` correct, `6` wrong, `3` bails, `0` errors.
- Recovered `financial` index `122`:
  `State the district and region for loan ID '4990'.`
- The generated SQL now projects `A2, A3` through the correct
  `district -> account -> loan` join path.

BIRD first-20 smoke:

- report: `v02-bird20-schema-desc-v1-report.json`
- `0/20`, `10` wrong, `9` bails, `1` structural error.
- This is expected: first-20 is dominated by metric/ranking/model gaps, not
  opaque schema-label gaps.

## Remaining Root Causes

- Metric/rate frames are still missing for FRPM/SAT-style derived formulas.
- Stage 3 still emits illegal value predicates in several rejected frames, such
  as `What` or markdown description text as values.
- Some schema-linker paths still fail closed when the graph has no useful
  candidates for the wording, e.g. `Q1 result` in formula_1.

The next high-leverage slice is MetricAtlas plus typed value/frame legality,
not more static examples.
