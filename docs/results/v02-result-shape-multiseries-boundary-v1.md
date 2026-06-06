# v0.2 Result Shape Multi-Series Boundary v1

Date: 2026-06-05

Validated SQL now carries the same practical visualization hint for two grouped
dimensions in both the native CLI and Python fallback tooling.

## Change

- Added `multi_series_chart` result-shape classification.
- Applies when validated SQL has `GROUP BY`, at least two non-measure
  dimensions, and at least one measure.
- Chooses a time-like dimension as `labels_from` when present; otherwise uses
  the first dimension.
- Emits `series_from` for the second dimension and Chart.js mapping metadata.
- Keeps table fallback because UI adapters still own rendering.

## Verification

- `cargo test -p semsql-cli`: pass, `46/46`.
- `cargo clippy -p semsql-cli --all-targets -- -D warnings`: pass.
- `uv run pytest python/semsql_eval/tests/test_cli.py -q -k "result_shape or llm_resolution_resolve_packet"`: pass, `11/11`.
- `uv run mypy python`: pass, `24` source files.

## Interpretation

This supports BI-style questions such as counts by day and region without
changing SQL generation. It is metadata over already-validated SQL, so it helps
table/chart plumbing while preserving the routing boundary.
