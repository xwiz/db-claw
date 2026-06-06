# v0.2 Real DB Postgres Typed Fallback Harness

Date: 2026-06-05

Superseded status: the harness is still useful implementation evidence, but
live typed-fallback proof now exists in
`v02-realdb-typed-fallback-postgres-disposable-v1.md`.

## Summary

- Added `python -m semsql_eval realdb-typed-fallback-postgres`.
- Added `python -m semsql_eval realdb-typed-fallback-postgres-suite`.
- These commands reuse the same randomized schema-derived typed-fallback
  runner as the MySQL/MariaDB suite, with a Postgres catalog adapter,
  Postgres extraction URL handling, Postgres SQL dialect rendering, and
  Postgres read-only execution.
- Supported probe families match the MySQL/MariaDB suite:
  `rate`, `grouped_avg`, `filtered_grouped_avg`,
  `value_filtered_grouped_avg`, `joined_filtered_grouped_avg`, and
  `multi_joined_filtered_grouped_avg`.
- Provider behavior remains unchanged: providers are optional, typed proposal
  only, and direct model SQL remains forbidden.

## Verification

- `uv run ruff check python/semsql_eval/src/semsql_eval/realdb_schema_probe.py python/semsql_eval/src/semsql_eval/__main__.py python/semsql_eval/tests/test_realdb_schema_probe.py python/semsql_eval/tests/test_cli.py`: pass
- `uv run mypy python`: pass
- `uv run pytest python/semsql_eval/tests/test_cli.py -q`: pass, `85` tests
- `uv run python -m semsql_eval realdb-typed-fallback-postgres --help`: pass
- `uv run python -m semsql_eval realdb-typed-fallback-postgres-suite --help`: pass
- Missing-URL smoke writes
  `docs/results/v02-realdb-typed-fallback-postgres-missing-url-smoke-v1.md`
  and fails closed with `missing_db_url`.

## Live Target Status

A previously shared server has Postgres `13.23` active with a large
`controlone` public schema. The harness was not run live because that host uses
peer auth for local sockets and ident auth for loopback TCP. Running the live
proof correctly requires a real read-only Postgres URL or a disposable
Postgres target, not changing auth on an existing application server.

The retained disposable proof uses the latter path.
