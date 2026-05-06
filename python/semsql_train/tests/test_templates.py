from __future__ import annotations

from semsql_train.templates import TemplateContext, expand


def _ctx(**overrides: object) -> TemplateContext:
    base = {
        "verb": "show",
        "entity": "students",
        "entity_canonical": "users",
    }
    base.update(overrides)
    return TemplateContext(**base)  # type: ignore[arg-type]


class TestFetch:
    def test_fetch_all_emits_select_star(self) -> None:
        out = expand(_ctx())
        intents = {row[0] for row in out}
        assert "fetch_all" in intents
        for intent, _nl, sql in out:
            if intent == "fetch_all":
                assert sql == "SELECT * FROM users"


class TestCount:
    def test_count_all_two_phrasings(self) -> None:
        out = expand(_ctx(aggregate="COUNT"))
        intents = {row[0] for row in out}
        assert "count_all" in intents
        assert "count_all_nl_alt" in intents


class TestFilter:
    def test_eq_filter(self) -> None:
        out = expand(
            _ctx(
                field="joined date",
                field_canonical="users.created_at",
                operator="=",
                value=":d",
            )
        )
        sqls = {row[2] for row in out}
        assert "SELECT * FROM users WHERE users.created_at = :d" in sqls

    def test_gt_filter_uses_over(self) -> None:
        out = expand(
            _ctx(
                field="balance",
                field_canonical="users.balance",
                operator=">",
                value="100",
            )
        )
        nls = {row[1] for row in out}
        assert "show students where balance over 100" in nls

    def test_lt_filter_uses_under(self) -> None:
        out = expand(
            _ctx(
                field="balance",
                field_canonical="users.balance",
                operator="<",
                value="100",
            )
        )
        nls = {row[1] for row in out}
        assert "show students where balance under 100" in nls


class TestEnum:
    def test_enum_subject_form(self) -> None:
        out = expand(
            _ctx(
                field="status",
                field_canonical="users.status_code",
                enum_label="active",
                enum_raw_value="2",
            )
        )
        nls = {row[1] for row in out}
        assert "active students" in nls
        assert "students who are active" in nls
        for _intent, _nl, sql in out:
            if "WHERE" in sql:
                assert "users.status_code = 2" in sql


class TestAggregate:
    def test_sum_field(self) -> None:
        out = expand(
            _ctx(
                aggregate="SUM",
                field="balance",
                field_canonical="users.balance",
            )
        )
        sqls = {row[2] for row in out}
        assert "SELECT SUM(users.balance) FROM users" in sqls


class TestOrderLimit:
    def test_top_n(self) -> None:
        out = expand(
            _ctx(
                field="balance",
                field_canonical="users.balance",
                limit=10,
                order_dir="DESC",
            )
        )
        sqls = {row[2] for row in out}
        assert "SELECT * FROM users ORDER BY users.balance DESC LIMIT 10" in sqls


class TestNoSpuriousMatches:
    def test_filter_template_silent_without_operator(self) -> None:
        # No operator → no filter template should fire.
        out = expand(_ctx(field="status", field_canonical="users.status_code"))
        intents = {row[0] for row in out}
        for forbidden in ("filter_eq", "filter_gt", "filter_lt"):
            assert forbidden not in intents
