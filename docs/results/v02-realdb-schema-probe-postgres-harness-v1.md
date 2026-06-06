# v0.2 Real DB Postgres Schema Probe Harness

Date: 2026-06-05

Superseded status: the harness is still useful implementation evidence, but
live proof now exists in
`v02-realdb-schema-probe-postgres-disposable-v1.md`.

## Summary

- Added `python -m semsql_eval realdb-schema-probe-postgres`.
- Added `python -m semsql_eval realdb-schema-probe-postgres-suite`.
- The probe reuses the existing schema-derived real-DB safety contract:
  schema-only extraction, count-only required execution, optional governed
  analytics diagnostics, no retained result values, and fail-closed behavior
  when no URL/driver is configured.
- Postgres discovery uses `current_schemas(false)`, mirrors the Rust extractor's
  `public` table-name convention, and keeps non-public schema-qualified names
  out of the first safe identifier proof instead of overclaiming support.
- Generated Postgres SQL shape validation now accepts standard double-quoted
  identifiers, so `SELECT AVG("orders"."amount") FROM "orders"` is classified
  like the equivalent MySQL/MariaDB governed shape.

## Verification

- `uv run ruff check python/semsql_eval/src/semsql_eval/realdb_schema_probe.py python/semsql_eval/src/semsql_eval/__main__.py python/semsql_eval/tests/test_realdb_schema_probe.py python/semsql_eval/tests/test_cli.py`: pass
- `uv run pytest python/semsql_eval/tests/test_realdb_schema_probe.py -q`: pass, `27` tests
- `uv run pytest python/semsql_eval/tests/test_cli.py -q`: pass, `82` tests
- `uv run mypy python`: pass
- `uv run python -m semsql_eval realdb-schema-probe-postgres --help`: pass
- `uv run python -m semsql_eval realdb-schema-probe-postgres-suite --help`: pass
- Missing-URL smoke writes
  `docs/results/v02-realdb-schema-probe-postgres-missing-url-smoke-v1.md` and
  fails closed with `missing_db_url`.

## Remote Read

A previously shared server has Postgres `13.23` active and a large
`controlone` public schema. No probe was run against it because its auth policy
uses peer auth for local sockets and ident for loopback TCP, so an SSH tunnel
does not provide a valid application DB URL without changing server auth or
creating credentials. The correct next proof needs a throwaway/read-only
Postgres URL, not a server mutation.

## Next

- Broaden the probe across more read-only/disposable Postgres schemas.
- Keep generated/disposable targets explicit with `--include-generated`.
