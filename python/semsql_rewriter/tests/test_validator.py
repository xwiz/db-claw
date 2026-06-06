from __future__ import annotations

import pytest
from semsql_rewriter.validator import ValidationError, validate


class TestStatementType:
    def test_accepts_simple_select(self) -> None:
        validate("SELECT 1")

    def test_accepts_cte(self) -> None:
        validate("WITH x AS (SELECT 1) SELECT * FROM x")

    def test_accepts_union(self) -> None:
        validate("SELECT 1 UNION SELECT 2")

    @pytest.mark.parametrize(
        "sql",
        [
            "INSERT INTO users (name) VALUES ('a')",
            "UPDATE users SET name = 'a'",
            "DELETE FROM users",
        ],
    )
    def test_rejects_dml(self, sql: str) -> None:
        with pytest.raises(ValidationError):
            validate(sql)

    @pytest.mark.parametrize(
        "sql",
        [
            "CREATE TABLE x (id int)",
            "DROP TABLE users",
            "ALTER TABLE users ADD COLUMN x int",
        ],
    )
    def test_rejects_ddl(self, sql: str) -> None:
        with pytest.raises(ValidationError):
            validate(sql)

    def test_rejects_multi_statement(self) -> None:
        with pytest.raises(ValidationError):
            validate("SELECT 1; SELECT 2")


class TestBannedFunctions:
    @pytest.mark.parametrize(
        "fn",
        [
            "pg_read_server_files('/etc/passwd')",
            "lo_import('/etc/passwd')",
            "load_file('/etc/passwd')",
            "pg_sleep(10)",
        ],
    )
    def test_rejects_banned(self, fn: str) -> None:
        with pytest.raises(ValidationError):
            validate(f"SELECT {fn}")

    def test_accepts_normal_functions(self) -> None:
        validate("SELECT count(*) FROM users")
        validate("SELECT lower(name) FROM users")
