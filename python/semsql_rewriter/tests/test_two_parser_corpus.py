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
