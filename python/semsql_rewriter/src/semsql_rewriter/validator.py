"""SQL validator — sqlglot-based AST allowlist.

The validator is the **first** of two parsers in the security pipeline. The
second is the Rust ``semsql-second-pass`` crate. Both must agree on every
post-rewrite SQL string; disagreement fails the build.

Allow-listed:

- ``SELECT`` statements (incl. CTEs that themselves resolve to SELECT).

Rejected at the AST level:

- DML: ``INSERT``, ``UPDATE``, ``DELETE``, ``MERGE``.
- DDL: ``CREATE``, ``DROP``, ``ALTER``, ``TRUNCATE``.
- Transaction control: ``BEGIN``, ``COMMIT``, ``ROLLBACK``, ``SAVEPOINT``.
- ``COPY``, ``CALL``, ``EXEC``.
- Functions with side effects (``pg_read_server_files``, ``lo_import``,
  ``dblink``, etc. — engine-specific deny-list).
- Multi-statement input.

Schema cross-checks and relationship-aware validation are layered around this
allowlist in the rewriter/graph reader path.

Reference: Apache Superset CVE-2025-48912.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

__all__ = [
    "ValidationError",
    "ValidationOptions",
    "validate",
]

# Function names with side effects, callable with no special syntax — must
# never appear in SemanticSQL-generated SQL. Add per-engine entries as new
# vectors are reported. Names are lower-cased before comparison.
_BANNED_FUNCTIONS: frozenset[str] = frozenset(
    {
        # Postgres
        "pg_read_server_files",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_sleep",
        "lo_import",
        "lo_export",
        "dblink",
        "dblink_exec",
        # MySQL
        "load_file",
        "sleep",
        "benchmark",
        # SQLite
        "load_extension",
        # MSSQL
        "xp_cmdshell",
        "openrowset",
        "opendatasource",
    }
)


class ValidationError(ValueError):
    """The provided SQL violated an invariant. Always fails closed."""


@dataclass(frozen=True)
class ValidationOptions:
    """Knobs surfaced to callers. Defaults are strict."""

    dialect: str = "postgres"
    allow_dml: bool = False
    allow_ddl: bool = False
    allow_multiple_statements: bool = False


def validate(sql: str, options: ValidationOptions | None = None) -> exp.Expression:
    """Parse ``sql`` and check every invariant. Returns the AST on success.

    Raises :class:`ValidationError` on any violation.
    """
    opts = options or ValidationOptions()

    parsed = sqlglot.parse(sql, read=opts.dialect)
    statements = [s for s in parsed if s is not None]
    if not statements:
        raise ValidationError("empty SQL input")
    if not opts.allow_multiple_statements and len(statements) > 1:
        raise ValidationError(f"multi-statement SQL not permitted (got {len(statements)})")

    for stmt in statements:
        _check_statement_type(stmt, opts)
        _check_no_writable_subtrees(stmt, opts)
        _check_no_banned_functions(stmt)

    # Schema cross-check (every table/column in the AST exists, every JOIN
    # is a known relationship edge) plugs in here once the SemanticGraph
    # reader is wired.

    return statements[0]


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _check_statement_type(stmt: exp.Expression, opts: ValidationOptions) -> None:
    if isinstance(stmt, (exp.Select, exp.Subquery, exp.Union, exp.Intersect, exp.Except)):
        return
    if isinstance(stmt, exp.With):
        # WITH x AS (SELECT ...) SELECT ... — must resolve to a query.
        body = stmt.this
        if isinstance(body, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
            return
        raise ValidationError(f"CTE body is not a SELECT: {type(body).__name__}")
    if not opts.allow_dml and isinstance(stmt, (exp.Insert, exp.Update, exp.Delete, exp.Merge)):
        raise ValidationError(f"DML statement not permitted: {type(stmt).__name__}")
    if not opts.allow_ddl and isinstance(stmt, (exp.Create, exp.Drop, exp.Alter)):
        raise ValidationError(f"DDL statement not permitted: {type(stmt).__name__}")
    raise ValidationError(f"non-SELECT statement: {type(stmt).__name__}")


def _check_no_writable_subtrees(stmt: exp.Expression, opts: ValidationOptions) -> None:
    """Reject DML/DDL hidden inside CTE bodies, subqueries, lateral joins, etc.

    Postgres allows ``WITH x AS (DELETE FROM users RETURNING *) SELECT * FROM x``
    — a CTE body can be a writable statement. The outer node looks like a
    ``With`` resolving to a ``Select``, so the top-level type check passes;
    the smuggling lives in the CTE body. We catch it by walking *every*
    descendant and rejecting any DML/DDL node that isn't the top-level
    statement (which has already been allowlisted by `_check_statement_type`).
    """
    for node in stmt.walk():
        if node is stmt:
            continue
        if not opts.allow_dml and isinstance(
            node, (exp.Insert, exp.Update, exp.Delete, exp.Merge)
        ):
            raise ValidationError(
                f"writable subtree smuggling: {type(node).__name__} inside SELECT/CTE"
            )
        if not opts.allow_ddl and isinstance(node, (exp.Create, exp.Drop, exp.Alter)):
            raise ValidationError(
                f"DDL subtree smuggling: {type(node).__name__} inside SELECT/CTE"
            )


def _check_no_banned_functions(stmt: exp.Expression) -> None:
    for func in _walk_functions(stmt):
        name = (func.name or "").lower()
        if name in _BANNED_FUNCTIONS:
            raise ValidationError(f"banned function: {name}")


def _walk_functions(stmt: exp.Expression) -> Iterable[exp.Func]:
    yield from (n for n in stmt.walk() if isinstance(n, exp.Func))
