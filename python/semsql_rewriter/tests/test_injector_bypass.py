"""Mandatory-filter bypass corpus — the 10 documented attack classes.

This is the v1.0 deployment gate. Any failure in this suite ships nothing.

Attack classes covered (drawn from Apache Superset CVE-2025-48912 patterns
and the bytebase RLS-footguns blog series):

 1. Subquery aliasing            — SELECT * FROM (SELECT * FROM users) u
 2. CTE re-binding               — WITH x AS (SELECT * FROM users) SELECT * FROM x
 3. UNION across scoped/unscoped — branches independently scoped
 4. Lateral / correlated subq    — both inner and outer scoped
 5. Recursive CTE                — every reference scoped
 6. Set operations (INTERSECT)   — every branch scoped
 7. Comment-based bypass         — comments stripped, no predicate hide
 8. Multi-statement smuggling    — rejected by validator (separate gate)
 9. Writable-CTE smuggling       — rejected by validator (separate gate)
10. DML/DDL smuggling            — rejected by validator (separate gate)

For each class: the injector either (a) produces SQL that scopes every
physical reference to a tenanted entity, or (b) raises an InjectorError
that fails closed. There is no third outcome.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

from semsql_rewriter.injector import ScopeRule, inject

_DIALECT = "sqlite"


def _users_rule() -> ScopeRule:
    return ScopeRule(
        entity="users",
        template="{{table}}.tenant_id = :tenant AND {{table}}.deleted_at IS NULL",
        required_params=("tenant",),
        source_rule="tenant_isolation",
    )


def _params() -> dict[str, str]:
    return {"tenant": "42"}


def _physical_user_refs(sql: str, dialect: str) -> list[exp.Table]:
    """Every physical Table node whose name is `users` in the parsed AST."""
    parsed = sqlglot.parse_one(sql, read=dialect)
    cte_aliases = {
        cte.alias_or_name.lower()
        for cte in parsed.find_all(exp.CTE)
        if cte.alias_or_name
    }
    return [
        t
        for t in parsed.find_all(exp.Table)
        if (t.name or "").lower() == "users" and (t.name or "").lower() not in cte_aliases
    ]


def _enclosing_select(table: exp.Table) -> exp.Select | None:
    cur: exp.Expression | None = table
    while cur is not None:
        if isinstance(cur, exp.Select):
            return cur
        cur = cur.parent
    return None


def _select_scopes_table(select: exp.Select, alias: str, dialect: str) -> bool:
    """True iff the Select's WHERE references the alias's tenant_id."""
    where = select.args.get("where")
    if where is None:
        return False
    target_col_sql = f"{alias}.tenant_id"
    return target_col_sql in where.sql(dialect=dialect).lower()


def _every_users_ref_is_scoped(
    sql: str, dialect: str = _DIALECT
) -> tuple[bool, list[str]]:
    """Audit helper — returns (ok, list_of_unscoped_aliases). Re-parses the
    rewritten SQL in the same dialect the injector emitted it in,
    otherwise paramstyle differences (`:tenant` vs `%(tenant)s`) trip
    the parser."""
    unscoped: list[str] = []
    for ref in _physical_user_refs(sql, dialect):
        alias = (ref.alias_or_name or ref.name).lower()
        select = _enclosing_select(ref)
        if select is None:
            unscoped.append(f"{alias}(no-enclosing-select)")
            continue
        if not _select_scopes_table(select, alias, dialect):
            unscoped.append(alias)
    return len(unscoped) == 0, unscoped


# ---------------------------------------------------------------------------
# 1. subquery aliasing
# ---------------------------------------------------------------------------


class TestSubqueryAliasing:
    def test_inner_subquery_users_scoped(self) -> None:
        result = inject(
            "SELECT * FROM (SELECT * FROM users) u",
            {"users": _users_rule()},
            _params(),
            dialect=_DIALECT,
        )
        ok, missing = _every_users_ref_is_scoped(result.sql)
        assert ok, f"unscoped: {missing}\nSQL: {result.sql}"


# ---------------------------------------------------------------------------
# 2. CTE re-binding
# ---------------------------------------------------------------------------


class TestCteRebinding:
    def test_cte_body_users_scoped(self) -> None:
        result = inject(
            "WITH x AS (SELECT * FROM users) SELECT * FROM x",
            {"users": _users_rule()},
            _params(),
            dialect=_DIALECT,
        )
        ok, missing = _every_users_ref_is_scoped(result.sql)
        assert ok, f"unscoped: {missing}\nSQL: {result.sql}"

    def test_cte_alias_reference_does_not_get_double_scoped(self) -> None:
        # The outer `SELECT * FROM x` uses x — a CTE alias, not a physical
        # users table — so x must NOT receive its own tenant_id predicate.
        result = inject(
            "WITH x AS (SELECT * FROM users) SELECT * FROM x",
            {"users": _users_rule()},
            _params(),
            dialect=_DIALECT,
        )
        sql = result.sql.lower()
        # Exactly one tenant_id predicate (in the CTE body).
        assert sql.count("tenant_id = :tenant") == 1, sql


# ---------------------------------------------------------------------------
# 3. UNION
# ---------------------------------------------------------------------------


class TestUnionBranches:
    def test_both_branches_scoped(self) -> None:
        result = inject(
            "SELECT id FROM users UNION ALL SELECT id FROM users",
            {"users": _users_rule()},
            _params(),
            dialect=_DIALECT,
        )
        ok, missing = _every_users_ref_is_scoped(result.sql)
        assert ok, f"unscoped: {missing}\nSQL: {result.sql}"


# ---------------------------------------------------------------------------
# 4. Lateral / correlated subquery
# ---------------------------------------------------------------------------


class TestCorrelatedSubquery:
    def test_inner_correlated_users_scoped(self) -> None:
        # Inner SELECT references outer u.id, but inner's `users u2` is its
        # own physical reference and must be scoped on its own.
        sql_in = (
            "SELECT u.id, (SELECT count(*) FROM users u2 WHERE u2.id = u.id) AS c "
            "FROM users u"
        )
        result = inject(sql_in, {"users": _users_rule()}, _params(), dialect=_DIALECT)
        ok, missing = _every_users_ref_is_scoped(result.sql)
        assert ok, f"unscoped: {missing}\nSQL: {result.sql}"


# ---------------------------------------------------------------------------
# 5. Recursive CTE
# ---------------------------------------------------------------------------


class TestRecursiveCte:
    def test_recursive_cte_users_scoped(self) -> None:
        # The recursive branch references the CTE alias (anchor), not users
        # again — so only the anchor branch's `users` needs scoping.
        sql_in = (
            "WITH RECURSIVE chain AS ("
            "  SELECT id, manager_id FROM users WHERE manager_id IS NULL "
            "  UNION ALL "
            "  SELECT u.id, u.manager_id FROM users u JOIN chain c ON u.manager_id = c.id"
            ") SELECT * FROM chain"
        )
        result = inject(sql_in, {"users": _users_rule()}, _params(), dialect=_DIALECT)
        ok, missing = _every_users_ref_is_scoped(result.sql)
        assert ok, f"unscoped: {missing}\nSQL: {result.sql}"


# ---------------------------------------------------------------------------
# 6. INTERSECT / EXCEPT
# ---------------------------------------------------------------------------


class TestSetOperations:
    def test_intersect_branches_scoped(self) -> None:
        result = inject(
            "SELECT id FROM users INTERSECT SELECT id FROM users",
            {"users": _users_rule()},
            _params(),
            dialect=_DIALECT,
        )
        ok, missing = _every_users_ref_is_scoped(result.sql)
        assert ok, f"unscoped: {missing}\nSQL: {result.sql}"

    def test_except_branches_scoped(self) -> None:
        # SQLite uses EXCEPT.
        result = inject(
            "SELECT id FROM users EXCEPT SELECT id FROM users",
            {"users": _users_rule()},
            _params(),
            dialect=_DIALECT,
        )
        ok, missing = _every_users_ref_is_scoped(result.sql)
        assert ok, f"unscoped: {missing}\nSQL: {result.sql}"


# ---------------------------------------------------------------------------
# 7. Comment-based bypass
# ---------------------------------------------------------------------------


class TestCommentBypass:
    def test_inline_comment_does_not_hide_table(self) -> None:
        # Comments must not let an attacker pretend a real table is a CTE.
        sql_in = "SELECT * FROM /* not a comment that helps */ users"
        result = inject(
            sql_in, {"users": _users_rule()}, _params(), dialect=_DIALECT
        )
        ok, missing = _every_users_ref_is_scoped(result.sql)
        assert ok, f"unscoped: {missing}\nSQL: {result.sql}"


# ---------------------------------------------------------------------------
# 8/9/10. Validator gates — these are tested by the validator suite, not
# the injector. We assert here that the validator does in fact reject them
# so the bypass corpus stays self-documenting.
# ---------------------------------------------------------------------------


class TestValidatorRejects:
    """These should never reach the injector. The validator gates them."""

    def test_multi_statement_rejected_by_validator(self) -> None:
        from semsql_rewriter.validator import ValidationError, validate

        import pytest

        with pytest.raises(ValidationError):
            validate("SELECT * FROM users; DROP TABLE users")

    def test_writable_cte_rejected_by_validator(self) -> None:
        from semsql_rewriter.validator import ValidationError, validate

        import pytest

        with pytest.raises(ValidationError):
            # Postgres-only writable CTE.
            validate("WITH d AS (DELETE FROM users RETURNING *) SELECT * FROM d")

    def test_dml_rejected_by_validator(self) -> None:
        from semsql_rewriter.validator import ValidationError, validate

        import pytest

        with pytest.raises(ValidationError):
            validate("DELETE FROM users WHERE id = 1")


# ---------------------------------------------------------------------------
# Hardening — identifier-shape variants that real DBs accept
# ---------------------------------------------------------------------------


class TestIdentifierVariants:
    """Case-folding and quoted-identifier shapes must all hit the same rule.

    The rule key is lower-cased; the injector lower-cases `table.name`
    before lookup. Any variant that bypasses this would let an attacker
    write `SELECT * FROM USERS` or `SELECT * FROM "users"` to escape
    scoping. These tests fail closed if the case-folding ever regresses.
    """

    def test_uppercase_identifier_matches_lowercase_rule(self) -> None:
        # SQLite's identifiers are case-insensitive by default; sqlglot
        # parses bare identifiers as a single name regardless of case.
        result = inject(
            "SELECT * FROM USERS",
            {"users": _users_rule()},
            _params(),
            dialect=_DIALECT,
        )
        assert "tenant_id = :tenant" in result.sql.lower()

    def test_quoted_identifier_matches_lowercase_rule(self) -> None:
        # Postgres quoted identifiers preserve case but the canonical
        # lookup uses the lower-cased name. We test against Postgres
        # specifically because that's where quoting matters most.
        # The Postgres dialect re-renders `:tenant` as `%(tenant)s`
        # (Python paramstyle); assert on the surface form sqlglot
        # actually emits.
        result = inject(
            'SELECT * FROM "users"',
            {"users": _users_rule()},
            _params(),
            dialect="postgres",
        )
        sql_lower = result.sql.lower()
        assert "tenant_id" in sql_lower and "tenant" in sql_lower
        ok, missing = _every_users_ref_is_scoped(result.sql, dialect="postgres")
        assert ok, f"unscoped: {missing}\nSQL: {result.sql}"

    def test_backticked_identifier_matches_lowercase_rule(self) -> None:
        result = inject(
            "SELECT * FROM `users`",
            {"users": _users_rule()},
            _params(),
            dialect="mysql",
        )
        assert "tenant_id = :tenant" in result.sql.lower()


# ---------------------------------------------------------------------------
# Hardening — deeply nested derived tables
# ---------------------------------------------------------------------------


class TestDeepNesting:
    """Three levels of derived-table nesting. Every physical reference at
    any depth must still be scoped — a regression here is the bug class
    that lets a multi-tenant leak hide behind enough subquery layers."""

    def test_three_level_nesting_users_scoped_at_innermost(self) -> None:
        sql_in = (
            "SELECT * FROM ("
            "  SELECT * FROM ("
            "    SELECT * FROM users"
            "  ) inner1"
            ") inner2"
        )
        result = inject(
            sql_in, {"users": _users_rule()}, _params(), dialect=_DIALECT
        )
        ok, missing = _every_users_ref_is_scoped(result.sql)
        assert ok, f"unscoped: {missing}\nSQL: {result.sql}"

    def test_self_join_scopes_both_sides(self) -> None:
        sql_in = (
            "SELECT u1.id, u2.id "
            "FROM users u1 JOIN users u2 ON u1.manager_id = u2.id"
        )
        result = inject(
            sql_in, {"users": _users_rule()}, _params(), dialect=_DIALECT
        )
        ok, missing = _every_users_ref_is_scoped(result.sql)
        assert ok, f"unscoped: {missing}\nSQL: {result.sql}"


# ---------------------------------------------------------------------------
# Hardening — audit log completeness
# ---------------------------------------------------------------------------


class TestAuditLog:
    """Every injection must be recorded. The audit log is the forensics
    surface — if an injection landed but no audit entry, post-incident
    review can't reconstruct what actually happened. Conversely, an
    audit entry without a real injection would mislead the on-call."""

    def test_one_audit_entry_per_physical_reference(self) -> None:
        result = inject(
            "SELECT u1.id, u2.id "
            "FROM users u1 JOIN users u2 ON u1.manager_id = u2.id",
            {"users": _users_rule()},
            _params(),
            dialect=_DIALECT,
        )
        # Two physical references → two audit entries.
        assert len(result.injected_predicates) == 2
        for alias, predicate, source in result.injected_predicates:
            assert alias in {"u1", "u2"}, alias
            assert "tenant_id = :tenant" in predicate
            assert source == "tenant_isolation"

    def test_no_audit_entry_when_predicate_is_idempotent(self) -> None:
        # Pre-injecting the same predicate user-side must not double-log.
        sql_with_existing = (
            "SELECT * FROM users "
            "WHERE users.tenant_id = :tenant "
            "AND users.deleted_at IS NULL"
        )
        result = inject(
            sql_with_existing,
            {"users": _users_rule()},
            _params(),
            dialect=_DIALECT,
        )
        # The injector recognised both leaves as already present and
        # emitted no new injections.
        assert result.injected_predicates == []
        # The query is still scoped end-to-end.
        ok, missing = _every_users_ref_is_scoped(result.sql)
        assert ok, f"unscoped: {missing}\nSQL: {result.sql}"


# ---------------------------------------------------------------------------
# Hardening — schema-qualified references (documents current behaviour)
# ---------------------------------------------------------------------------


class TestSchemaQualified:
    """Multi-schema references match the rule by the table's bare name.

    This documents a deliberate trade-off: the SemanticGraph collapses
    `public.users` → canonical `users` at extract time, so the injector
    keys on bare names too. Cross-schema collisions (`public.users` vs
    `audit.users` both having a `tenant_id`) are an edge case that
    `semsql doctor` surfaces in the conflict log so the deployer
    splits them via per-schema rule keys.
    """

    def test_public_qualified_users_is_scoped(self) -> None:
        result = inject(
            "SELECT * FROM public.users",
            {"users": _users_rule()},
            _params(),
            dialect="postgres",
        )
        ok, missing = _every_users_ref_is_scoped(result.sql, dialect="postgres")
        assert ok, f"unscoped: {missing}\nSQL: {result.sql}"
