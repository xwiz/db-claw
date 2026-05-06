# Intent Pattern Library

Stage 0b's deterministic answer to "how does an LLM know that 'bleeding money' means look at expenses?".

The library is an **additive bias layer**. A matched pattern can only *prefer* schema items the SemanticGraph already exposes; it cannot create a column or a filter the validator wouldn't accept.

## Adding a pattern

1. Edit [`patterns.yaml`](patterns.yaml). Each entry needs at minimum a `pattern` (PCRE) and `intent_type` (stable identifier).
2. Add a fixture test in `python/semsql_eval/src/semsql_eval/intent_fixtures.yaml` covering both a positive case (the pattern fires when expected) and at least one negative case (the pattern does not fire on look-alike queries).
3. Open a PR. CI runs the fixtures + per-stage eval — patterns that drop end-to-end accuracy on Spider/BIRD are bounced.

## Why YAML, not Python

So the library is curatable by non-engineers (product managers, support engineers, analysts close to the user vocabulary) and so contributors don't need to rebuild the cascade to ship a new idiom.

## Schema

| Field            | Type    | Required | Description                                                              |
| ---------------- | ------- | -------- | ------------------------------------------------------------------------ |
| `pattern`        | string  | yes      | PCRE, case-insensitive at runtime. Escape backslashes for YAML.          |
| `intent_type`    | string  | yes      | Stable identifier — shared with `IntentReference` in the SemanticGraph.  |
| `column_hints`   | list    | no       | Canonical or label-style column names Stage 1 should boost.              |
| `ordering`       | enum    | no       | `ASC` or `DESC`. Feeds the ORDER BY slot.                                |
| `default_limit`  | int     | no       | Feeds the LIMIT slot when no explicit count is parsed.                   |
| `comparator`     | string  | no       | SQL comparator + literal, e.g. `"< 0"`.                                  |
| `description`    | string  | no       | Single-line summary surfaced by `semsql doctor`.                         |

## Reference

- The matcher implementation: [`crates/semsql-intent/src/lib.rs`](../crates/semsql-intent/src/lib.rs).
- The Stage 0b position in the runtime pipeline: [`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md).
