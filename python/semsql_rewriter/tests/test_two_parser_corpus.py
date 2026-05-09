"""Generate fixtures for the two-parser-must-agree differential test.

The Python rewriter (sqlglot) produces rewritten SQL; the Rust second-pass
(sqlparser-rs) must accept that same SQL with the same scope invariants.
Two parsers must agree.

This module:
  1. Runs the injector on every query in the bypass corpus.
  2. Writes ``crates/semsql-second-pass/tests/fixtures/two_parser_corpus.jsonl``
     — one JSON record per case, with the rewritten SQL and the scope
     invariants the Rust side should re-verify.
  3. Asserts the file is up-to-date in CI (regeneration is committed).

Each line is::

    {"sql": "<rewritten SQL>", "scope": {"users": "tenant_id"},
     "should_pass": true, "name": "subquery_aliasing"}
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from semsql_rewriter.injector import ScopeRule, inject

_FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "crates"
    / "semsql-second-pass"
    / "tests"
    / "fixtures"
    / "two_parser_corpus.jsonl"
)
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


def _cases() -> list[dict[str, object]]:
    rules = {"users": _users_rule()}
    out: list[dict[str, object]] = []
    for name, sql_in in _CORPUS:
        result = inject(sql_in, rules, _params(), dialect=_DIALECT)
        out.append(
            {
                "name": name,
                "input_sql": sql_in,
                "sql": result.sql,
                "scope": {"users": "tenant_id"},
                "should_pass": True,
            }
        )
    # Negative-control cases: hand-written SQL that *should* be rejected by
    # the Rust scope-leak walker even after injector logic runs (or doesn't).
    # Every entry here is one of the 10 documented bypass classes from
    # Plan §Verification#8 — when the Rust second-pass passes one of these,
    # CI fails closed.
    out.extend(
        [
            {
                "name": "negative_unscoped_simple",
                "input_sql": None,
                "sql": "SELECT * FROM users",
                "scope": {"users": "tenant_id"},
                "should_pass": False,
            },
            {
                "name": "negative_subquery_aliasing_unscoped",
                "input_sql": None,
                "sql": "SELECT * FROM (SELECT * FROM users) u",
                "scope": {"users": "tenant_id"},
                "should_pass": False,
            },
            # UNION across one scoped + one unscoped branch — the Rust
            # walker must inspect every branch independently.
            {
                "name": "negative_union_one_branch_unscoped",
                "input_sql": None,
                "sql": (
                    "SELECT id FROM users WHERE users.tenant_id = :tenant "
                    "UNION ALL SELECT id FROM users"
                ),
                "scope": {"users": "tenant_id"},
                "should_pass": False,
            },
            # Self-join with one alias scoped, the other not — common
            # injector-omission pattern when alias rewriting is buggy.
            {
                "name": "negative_self_join_one_alias_unscoped",
                "input_sql": None,
                "sql": (
                    "SELECT u1.id, u2.id FROM users u1 "
                    "JOIN users u2 ON u1.manager_id = u2.id "
                    "WHERE u1.tenant_id = :tenant"
                ),
                "scope": {"users": "tenant_id"},
                "should_pass": False,
            },
            # Correlated subquery whose inner `users` reference lacks
            # scope. Outer is fine; inner must be checked too.
            {
                "name": "negative_correlated_inner_unscoped",
                "input_sql": None,
                "sql": (
                    "SELECT u.id, (SELECT COUNT(*) FROM users u2 WHERE u2.id = u.id) "
                    "AS c FROM users u WHERE u.tenant_id = :tenant"
                ),
                "scope": {"users": "tenant_id"},
                "should_pass": False,
            },
            # Multi-statement: second statement is unscoped DML/SELECT.
            # The select-only invariant fires before any scope check —
            # the Rust pass must reject this on statement-count alone.
            {
                "name": "negative_multi_statement",
                "input_sql": None,
                "sql": (
                    "SELECT * FROM users WHERE users.tenant_id = :tenant; "
                    "SELECT * FROM users"
                ),
                "scope": {"users": "tenant_id"},
                "should_pass": False,
            },
            # DML/DDL smuggling — the Rust pass must reject anything
            # that isn't a SELECT/Query statement.
            {
                "name": "negative_dml",
                "input_sql": None,
                "sql": "DELETE FROM users WHERE id = 1",
                "scope": {"users": "tenant_id"},
                "should_pass": False,
            },
            # Deep nesting with the innermost layer missing scope.
            # Validates the walker recurses into derived tables.
            {
                "name": "negative_three_level_inner_unscoped",
                "input_sql": None,
                "sql": (
                    "SELECT * FROM (SELECT * FROM (SELECT * FROM users) i1 "
                    "WHERE i1.tenant_id = :tenant) i2"
                ),
                "scope": {"users": "tenant_id"},
                "should_pass": False,
            },
        ]
    )
    return out


_CORPUS: list[tuple[str, str]] = [
    ("simple_from", "SELECT * FROM users"),
    ("aliased_from", "SELECT * FROM users u"),
    ("join", "SELECT * FROM users JOIN users u2 ON u2.id = users.id"),
    ("subquery_aliasing", "SELECT * FROM (SELECT * FROM users) u"),
    ("cte_rebinding", "WITH x AS (SELECT * FROM users) SELECT * FROM x"),
    (
        "union_branches",
        "SELECT id FROM users UNION ALL SELECT id FROM users",
    ),
    (
        "correlated_subquery",
        "SELECT u.id, (SELECT count(*) FROM users u2 WHERE u2.id = u.id) AS c FROM users u",
    ),
    (
        "intersect_branches",
        "SELECT id FROM users INTERSECT SELECT id FROM users",
    ),
    (
        "except_branches",
        "SELECT id FROM users EXCEPT SELECT id FROM users",
    ),
    # Hardening cases — every shape that real DBs accept and that the
    # injector now scopes correctly per the bypass-corpus tests.
    ("uppercase_identifier", "SELECT * FROM USERS"),
    (
        "three_level_nesting",
        "SELECT * FROM (SELECT * FROM (SELECT * FROM users) inner1) inner2",
    ),
    (
        "self_join_aliased",
        "SELECT u1.id, u2.id FROM users u1 JOIN users u2 ON u1.manager_id = u2.id",
    ),
    (
        "cte_recursive",
        "WITH RECURSIVE chain AS ("
        " SELECT id, manager_id FROM users WHERE manager_id IS NULL "
        "UNION ALL "
        " SELECT u.id, u.manager_id FROM users u JOIN chain c ON u.manager_id = c.id"
        ") SELECT * FROM chain",
    ),
    (
        "comment_inline",
        "SELECT * FROM /* attempt at hiding */ users",
    ),
    # ----- Hardening expansion (Verification#8 round 2) ------------------
    # `IN (SELECT ... FROM users)` — the inner subquery's `users` reference
    # is the one that needs scoping. Plan §Verification#8 calls out
    # subquery-aliasing; the IN form is its predicate-context cousin.
    (
        "in_subquery",
        "SELECT id FROM users WHERE id IN (SELECT id FROM users)",
    ),
    # `EXISTS (SELECT 1 FROM users WHERE ...)` — same shape, different
    # predicate operator.
    (
        "exists_subquery",
        "SELECT id FROM users WHERE EXISTS (SELECT 1 FROM users)",
    ),
    # Trailing line comment must not let an attacker paper over a missing
    # predicate. The injector strips comments before walking the AST so
    # the rewritten SQL has the predicate in scope regardless of what the
    # input commentary said.
    (
        "trailing_line_comment",
        "SELECT * FROM users -- pretend tenant_id is already filtered",
    ),
]


def test_corpus_is_committed_and_up_to_date() -> None:
    expected = "\n".join(json.dumps(c, sort_keys=True) for c in _cases()) + "\n"
    if not _FIXTURE.exists():
        _FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        _FIXTURE.write_text(expected, encoding="utf-8")
        pytest.fail(
            f"fixture file {_FIXTURE} did not exist — created it. "
            "Re-run the Rust integration test."
        )
    actual = _FIXTURE.read_text(encoding="utf-8")
    if actual != expected:
        _FIXTURE.write_text(expected, encoding="utf-8")
        pytest.fail(
            f"fixture file {_FIXTURE} was stale and has been regenerated. "
            "Commit the change."
        )
