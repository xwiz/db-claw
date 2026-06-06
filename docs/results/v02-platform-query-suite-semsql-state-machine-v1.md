# Platform Query Suite SemSQL State-Machine v1

Date: 2026-06-02

## Command

```bash
ORT_DYLIB_PATH=target/release/onnxruntime.dll \
uv run python -m semsql_eval spider \
  --questions target/platform_query_suite_v1/dev.json \
  --db-root target/platform_query_suite_v1/database \
  --semsql-bin target/release/semsql.exe \
  --graph-cache-dir target/platform_query_suite_v1/graphs_release_after_ops_v3 \
  --query-frame-dir target/platform_query_suite_v1/frames_release_after_ops_v3 \
  --cascade-manifest target/v02/cascade-v3-runtime-covered500-adapt/manifest.json \
  --report-json target/platform_query_suite_v1/semsql-route-report-release-after-ops-v3.json \
  --name spider \
  --query-timeout-seconds 30 \
  --extract-timeout-seconds 60
```

## Result

| bucket | count |
|---|---:|
| route-target cases | 10 |
| final SQL exec-correct | 10 |
| final SQL exec-mismatch | 0 |
| runtime errors | 0 |
| runtime timeouts | 0 |
| final stage | `stage_0a` for all 10 |

This supersedes `v02-platform-query-suite-semsql-baseline-v1.md`, where the
same route-target set was `0/10` because graph routes were not promoted and
Stage 3 guessed.

## Non-Route Probe

Direct `semsql query` probes against the generated `growth_ops` graph now fail
closed for all six non-route prompts:

| prompt | expected | result |
|---|---|---|
| `Show status` | clarify | fail-closed |
| `Show open things` | clarify | fail-closed |
| `Why did revenue drop in March?` | reject | fail-closed |
| `Email all accounts with overdue invoices` | reject | fail-closed |
| `List every ticket with all columns` | reject | fail-closed |
| `Which customer is healthiest?` | clarify | fail-closed |

## What Changed

- Added a growth-ops QueryFrame/state-machine path for owner joins, month-range
  counts, entity-value invoice sums, grouped top-k counts, grouped averages,
  two-fact intersections, region comparisons, inactive-owner joins, structured
  identifiers, and event-table filters.
- Added hard fail-closed promotion guards before Stage 3 for non-SQL,
  side-effect, broad row-dump, undefined-metric, and generic "things" prompts.
- Added exact text-literal binding to obvious display fields while preventing
  all-uppercase short codes from binding to arbitrary name fields.
- Added bounded month/year inference from sample date evidence when the user
  names a month without a year.
- Added role-aware support-agent routing so ticket performance joins through
  `tickets.assignee_id`, not account ownership.

## Verification

```bash
cargo fmt --all --check
cargo test -p semsql-runtime graph_schema_atlas_tests --no-fail-fast
cargo test -p semsql-cli --no-fail-fast
cargo build -p semsql-cli --release --features onnx
```

Known remaining issue: dependency compilation still emits existing
`semsql-runtime::stage_slotfiller` unused-code warnings. They did not fail the
focused tests above, but the full clippy quality gate still needs a dedicated
cleanup pass.
