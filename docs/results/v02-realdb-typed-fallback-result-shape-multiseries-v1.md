# v0.2 Real-DB Typed Fallback Result Shape + Multi-Series

Date: 2026-06-05

## Change

Real-DB typed fallback probes now require accepted cases to carry a usable
`result_shape`. Selected SQL without shape evidence fails the per-run and suite
summary.

Added a schema-derived BI probe family:
`multi_series_grouped_avg`. It selects metric + date/time + categorical
dimension from the same table and asks for "metric by dimension over time"
without app-specific phrase maps.

## Boundary

- `conditional_rate` must shape as `scalar_metric`.
- grouped average families must shape as chartable grouped output.
- `multi_series_grouped_avg` must shape as `multi_series_chart`.
- The SQL matcher requires the metric, time field, and segment field to appear
  in both the selected output and `GROUP BY` where applicable.

This is not a provider-SQL path. Providers still return typed plans only; local
validation/rendering/execution owns SQL.

## Live Local Probe

Local MariaDB `fraud_radar`, family `multi_series_grouped_avg`, probe count `2`.
The first run exposed wrong accepted local SQL: `AVG(updated_by)` / `AVG(assigned_by)`
for questions asking average amount by status over a time field. The shape guard
first demoted both local attempts before execution:

- selected SQL: `0/2`
- typed fallback selected: `0/2`
- rejected packets with schema evidence: `2/2`
- route reason: `local_route_shape_mismatch:requested_multi_series_time_dimension`

Runtime recovery then added generic field-aware `over <date-field>` grouping and
actor/audit numeric measure rejection. New local run:

- selected SQL: `2/2`
- execution ok: `2/2`
- expected table/field matches: `2/2`
- result shape ok: `2/2`, all `multi_series_chart`

No provider SQL is accepted; this is native local SQL validated against schema
and result shape.

## Verification

```bash
uv run pytest python/semsql_eval/tests/test_realdb_schema_probe.py -q -k "typed_fallback"
uv run pytest python/semsql_eval/tests/test_cli.py -q -k "realdb_typed_fallback or typed_fallback_multi_series or result_shape"
uv run pytest python/semsql_eval/tests/test_cli.py -q -k "demotes_local_shape_mismatch or realdb_typed_fallback or typed_fallback_multi_series or result_shape"
uv run pytest python/semsql_eval/tests/test_cli.py python/semsql_eval/tests/test_realdb_schema_probe.py -q
uv run ruff check python/semsql_eval/src/semsql_eval/__main__.py python/semsql_eval/src/semsql_eval/realdb_schema_probe.py python/semsql_eval/tests/test_cli.py python/semsql_eval/tests/test_realdb_schema_probe.py
uv run mypy python
cargo test -p semsql-runtime graph_schema_atlas_tests -- --nocapture
cargo clippy -p semsql-runtime --all-targets --features onnx -- -D warnings
```

Results: focused schema-probe `12/12`, focused CLI `15/15`, broader two-file
suite `116/116`, demotion-focused CLI `16/16`, runtime atlas `119/119`,
Ruff pass, mypy pass, Clippy pass. Live MariaDB v2: `2/2` selected,
`2/2` executed, `2/2` result-shape ok. Broader MariaDB v2:
`15/15` selected, executed, expected-field matched, and shape matched across
five schemas.
