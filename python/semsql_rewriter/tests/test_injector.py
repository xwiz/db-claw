"""Mandatory-filter injector — basic invariants.

The bypass corpus (10 documented attack classes) lives in
``test_injector_bypass.py`` and is the v1.0 deployment gate.
"""

from __future__ import annotations

import pytest

from semsql_rewriter.injector import (
    AuditLogWriter,
    InjectionResult,
    InjectorError,
    ScopeRule,
    inject,
)


def _users_rule(entity: str = "users") -> ScopeRule:
    return ScopeRule(
        entity=entity,
        template="{{table}}.tenant_id = :tenant AND {{table}}.deleted_at IS NULL",
        required_params=("tenant",),
        source_rule="tenant_isolation",
    )


def _params() -> dict[str, str]:
    return {"tenant": "42"}


def _normalize(sql: str) -> str:
    return " ".join(sql.split()).lower()


# Tests use sqlite dialect because it preserves the `:name` placeholder form.
# Postgres rewrites `:name` to `%(name)s` (psycopg-style); we still test that
# Postgres rendering separately in TestDialectRendering.
_DIALECT = "sqlite"


def _go(sql: str, rules: dict[str, ScopeRule] | None = None) -> str:
    rules = rules or {"users": _users_rule()}
    result = inject(sql, rules, _params(), dialect=_DIALECT)
    return _normalize(result.sql)


class TestRequiredParams:
    def test_missing_param_raises(self) -> None:
        with pytest.raises(InjectorError):
            inject("SELECT * FROM users", {"users": _users_rule()}, {})

    def test_all_params_present_succeeds(self) -> None:
        result = inject(
            "SELECT * FROM users", {"users": _users_rule()}, _params(), dialect=_DIALECT
        )
        assert isinstance(result, InjectionResult)


class TestTemplateSafety:
    def test_template_without_table_placeholder_rejected(self) -> None:
        bad = ScopeRule(
            entity="users",
            template="tenant_id = :tenant",
            required_params=("tenant",),
        )
        with pytest.raises(InjectorError):
            inject("SELECT * FROM users", {"users": bad}, _params(), dialect=_DIALECT)

    def test_template_missing_declared_param_rejected(self) -> None:
        bad = ScopeRule(
            entity="users",
            template="{{table}}.tenant_id = 1",
            required_params=("tenant",),
        )
        with pytest.raises(InjectorError):
            inject("SELECT * FROM users", {"users": bad}, _params(), dialect=_DIALECT)


class TestBasicInjection:
    def test_simple_from_gets_scope(self) -> None:
        sql = _go("SELECT * FROM users")
        assert "users.tenant_id = :tenant" in sql
        assert "users.deleted_at is null" in sql

    def test_explicit_alias_uses_alias_in_predicate(self) -> None:
        sql = _go("SELECT * FROM users u")
        assert "u.tenant_id = :tenant" in sql

    def test_unrelated_entity_unscoped(self) -> None:
        sql = _go("SELECT * FROM posts")
        assert "tenant_id" not in sql

    def test_audit_log_records_injection(self) -> None:
        result = inject(
            "SELECT * FROM users", {"users": _users_rule()}, _params(), dialect=_DIALECT
        )
        assert len(result.injected_predicates) == 1
        alias, predicate, source_rule = result.injected_predicates[0]
        assert alias == "users"
        assert "tenant_id" in predicate
        assert source_rule == "tenant_isolation"


class TestIdempotence:
    def test_already_scoped_query_not_double_injected(self) -> None:
        sql_in = (
            "SELECT * FROM users "
            "WHERE users.tenant_id = :tenant AND users.deleted_at IS NULL"
        )
        result = inject(sql_in, {"users": _users_rule()}, _params(), dialect=_DIALECT)
        normalised = _normalize(result.sql)
        assert normalised.count("tenant_id = :tenant") == 1
        assert normalised.count("deleted_at is null") == 1
        assert result.injected_predicates == []


class TestJoins:
    def test_join_target_gets_scope(self) -> None:
        rules = {"users": _users_rule(), "posts": _users_rule("posts")}
        sql = _go(
            "SELECT * FROM users JOIN posts ON posts.author_id = users.id", rules=rules
        )
        assert "users.tenant_id = :tenant" in sql
        assert "posts.tenant_id = :tenant" in sql

    def test_join_unrelated_entity_only_scopes_known(self) -> None:
        sql = _go("SELECT * FROM users JOIN audit_logs a ON a.user_id = users.id")
        assert "users.tenant_id" in sql
        assert "audit_logs.tenant_id" not in sql


class TestDialectRendering:
    def test_postgres_renders_named_param_in_psycopg_form(self) -> None:
        # Sanity check: postgres dialect rewrites :tenant → %(tenant)s.
        # That's the contract psycopg consumers expect.
        result = inject(
            "SELECT * FROM users", {"users": _users_rule()}, _params(), dialect="postgres"
        )
        assert "%(tenant)s" in result.sql


class TestAuditLogWriter:
    """Persistence surface for the in-memory audit trail."""

    def test_records_one_line_per_injection(self, tmp_path) -> None:
        import json

        result = inject(
            "SELECT u1.id FROM users u1 JOIN users u2 ON u1.id = u2.id",
            {"users": _users_rule()},
            _params(),
            dialect="sqlite",
        )
        log = tmp_path / "audit.jsonl"
        writer = AuditLogWriter(log)
        n = writer.record("req-42", result)
        assert n == 2  # two physical references → two entries

        lines = log.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        for line in lines:
            entry = json.loads(line)
            assert entry["query_id"] == "req-42"
            assert entry["alias"] in {"u1", "u2"}
            assert "tenant_id" in entry["predicate"]
            assert entry["source_rule"] == "tenant_isolation"
            assert entry["timestamp_utc"]  # ISO-8601 UTC

    def test_appends_across_calls(self, tmp_path) -> None:
        log = tmp_path / "audit.jsonl"
        writer = AuditLogWriter(log)
        for qid in ["req-1", "req-2", "req-3"]:
            r = inject(
                "SELECT * FROM users",
                {"users": _users_rule()},
                _params(),
                dialect="sqlite",
            )
            writer.record(qid, r)
        lines = log.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3
        import json
        ids = [json.loads(l)["query_id"] for l in lines]
        assert ids == ["req-1", "req-2", "req-3"]

    def test_no_entries_when_query_already_scoped(self, tmp_path) -> None:
        log = tmp_path / "audit.jsonl"
        writer = AuditLogWriter(log)
        r = inject(
            "SELECT * FROM users WHERE users.tenant_id = :tenant "
            "AND users.deleted_at IS NULL",
            {"users": _users_rule()},
            _params(),
            dialect="sqlite",
        )
        n = writer.record("idempotent", r)
        assert n == 0
        # File created but empty.
        assert log.exists()
        assert log.read_text(encoding="utf-8") == ""
