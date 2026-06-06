# v0.2 Live OpenAI Typed Fallback Shape Contract v1

Date: 2026-06-05

Purpose: prove a real rejected BI/ops query can recover through a live provider
typed plan while SemSQL keeps SQL rendering, shape validation, and read-only
execution local.

## Case

- graph: `target/v02/realdb-typed-fallback-mariadb-multiseries-broaden-v2/hostshell/graphs/hostshell.schemaonly.semsql`
- question: `show invoice count by tenant over created at`
- local result: rejected, `needs_model`
- provider: OpenAI Responses API, typed proposal only
- direct provider SQL used: `False`

## Result

OpenAI selected `invoices`, `tenants`, `invoices.created_at`,
`tenants.name`, and `invoices.tenant_id -> tenants.id`, but initially asked for
a time grain. The generic fix promotes only this safe case: when the provider
asks for grain but the user explicitly names a date/time field, SemSQL renders
the raw field grouping. Vague `over time` queries still clarify.

Selected SQL:

```sql
SELECT `invoices`.`created_at`, `tenants`.`name`, COUNT(`invoices`.`id`) AS `invoice_count`
FROM `invoices`
JOIN `tenants` ON `invoices`.`tenant_id` = `tenants`.`id`
GROUP BY `invoices`.`created_at`, `tenants`.`name`
```

Shape: `multi_series_chart`. Render issue:
`clarify_auto_promoted_explicit_time_grain`.

## Execution

Saved provider proposal was replayed against local MariaDB with read-only
execution and row retention disabled:

- status: `ok`
- engine: `mysql`
- columns: `created_at`, `name`, `invoice_count`
- rows retained: `False`
- selected source: `typed_fallback`

Artifacts:

- live provider: `target/v02/live-provider-openai-shape-contract/hostshell-invoice-tenant-over-created-v3`
- execution replay: `target/v02/live-provider-openai-shape-contract/hostshell-invoice-tenant-over-created-exec-v2`

## Verification

- `uv run pytest python/semsql_eval/tests/test_llm_resolution.py -q`: pass,
  `67/67`.
- `uv run pytest python/semsql_eval/tests/test_cli.py -q -k "llm_resolution or fallback or result_shape or typed_fallback"`:
  pass, `48/48`.
- `uv run pytest python/semsql_eval/tests/test_realdb_schema_probe.py -q -k "typed_fallback or result_shape or multi_series"`:
  pass, `12/12`.
- `uv run ruff check ...`: pass.
- `uv run mypy python`: pass, `24` source files.

## Boundary

This is one live-provider proof, not broad provider readiness. Next provider
work should run a batch of rejected real-app BI/ops packets across join,
filtered metric, date-window, and safe-clarify cases.
