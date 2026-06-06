# v0.2 Span-Role Safety Slice

Date: 2026-06-02

## Summary

This slice implements the first fix from
`v02-bird-failure-root-cause-cleaned.md`: stop promoting frames where a field,
metric, or question span has been misclassified as a predicate value.

It also removes the old assumption that every list projection should use
`DISTINCT`. Lists now preserve row multiplicity unless the user asks for
distinct/unique/deduplicated values.

## Runtime Changes

- Added a span-role conflict gate for runtime QueryFrame promotion.
  - Blocks question words such as `What` from becoming literal values.
  - Blocks acronym/field mentions such as `Q1 result` or `SAT score` from
    becoming unrelated code predicates.
  - Blocks metric dimension phrases such as `score in Writing` from becoming
    arbitrary categorical predicates.
  - Still permits explicit code/id lookups such as `with code Q1`.
- Made unpromoted routed frames with span-role conflicts fail closed before
  model fallback.
- Changed QueryFrame projection rendering so `SELECT DISTINCT` is emitted only
  for explicit unique/distinct requests.
- Changed model-list DISTINCT repair to the same explicit-uniqueness contract.

## Verification

```text
cargo test -p semsql-runtime --features onnx --lib --no-fail-fast
cargo clippy -p semsql-runtime --features onnx --lib --tests -- -D warnings
cargo build -p semsql-cli --release --features semsql-cli/onnx
python scripts\check_git_artifacts.py --all
```

All passed.

Product canary:

- `v02-queryframe-canary-suite-spanrole-v1.md`
- status `PASS`
- `9/9` runs
- `144/144` routed exec accuracy
- `18/18` reject fail-closed

Focused BIRD failure trace:

- Before: `13` examples, `2` correct, `11` wrong, `0` bails.
- After: `13` examples, `3` correct, `7` wrong, `3` bails.

The extra correct case is the `csgillespie` badges query, which needed row
multiplicity preserved. The new bails are unsafe span-role cases (`SAT`, `Q1`,
and `Writing`) that previously produced wrong SQL.

BIRD first-20 smoke:

- Before cleaned span-role slice: `0` correct, `19` wrong, `1` structural error.
- After span-role slice: `0` correct, `10` wrong, `9` bails, `1` structural
  error.

This is not an accuracy milestone yet. It is a safety and evidence milestone:
bad SQL was converted into fail-closed behavior. The next accuracy milestone is
MetricAtlas plus multi-projection/complete-join QueryFrames.
