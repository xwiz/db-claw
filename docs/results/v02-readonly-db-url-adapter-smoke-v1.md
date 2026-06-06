# Read-Only DB-URL Adapter Smoke

Date: 2026-06-04

## Scope

This is a live execution-adapter smoke, not an NL-to-SQL benchmark.

- command family: `python -m semsql_eval` helper path via
  `_execute_selected_db_url`
- environment: `uv run --extra db`
- target: local MariaDB on `127.0.0.1:3306`, URL credentials redacted
- SQL: `SELECT 1 AS ok`
- execution policy: single read-only `SELECT`, bounded preview rows, timeout
- result: `ok = 1`
- status: pass

## Interpretation

The optional `db` extra installs the MariaDB/Postgres drivers, and the
MariaDB adapter can open a configured connection and execute a read-only query
through the same selected-SQL execution policy used by the product packet
resolver.

This does not prove production NL-to-SQL readiness on MariaDB schemas. The next
required proof is a real-schema run using generated SemSQL queries or typed
fallback SQL against a read-only application database user.
