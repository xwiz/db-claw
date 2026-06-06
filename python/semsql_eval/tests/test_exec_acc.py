from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from semsql_eval.exec_acc import exec_eq, execute


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "t.sqlite"
    conn = sqlite3.connect(p)
    conn.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, status INTEGER);
        INSERT INTO users VALUES (1, 'a', 1), (2, 'b', 2), (3, 'c', 2);
        """
    )
    conn.commit()
    conn.close()
    return p


class TestExecute:
    def test_returns_rows(self, db: Path) -> None:
        r = execute(db, "SELECT id FROM users ORDER BY id")
        assert r.rows == ((1,), (2,), (3,))
        assert r.column_count == 1

    def test_captures_error_without_raising(self, db: Path) -> None:
        r = execute(db, "SELECT * FROM nonexistent")
        assert r.is_error
        assert r.rows == ()

    def test_interrupts_long_running_query(self, db: Path) -> None:
        r = execute(
            db,
            "WITH RECURSIVE cnt(x) AS ("
            "SELECT 1 UNION ALL SELECT x + 1 FROM cnt WHERE x < 100000000"
            ") SELECT sum(x) FROM cnt",
            timeout_seconds=0.001,
        )

        assert r.timed_out
        assert r.error == "sqlite execution timed out"


class TestExecEq:
    def test_unordered_match(self, db: Path) -> None:
        gold = "SELECT id FROM users WHERE status = 2"
        pred = "SELECT id FROM users WHERE status = 2 LIMIT 100"
        assert exec_eq(db, gold, pred)

    def test_unordered_mismatch(self, db: Path) -> None:
        gold = "SELECT id FROM users WHERE status = 2"
        pred = "SELECT id FROM users"
        assert not exec_eq(db, gold, pred)

    def test_order_sensitive_when_gold_has_order_by(self, db: Path) -> None:
        gold = "SELECT id FROM users ORDER BY id"
        pred_correct = "SELECT id FROM users ORDER BY id"
        pred_wrong = "SELECT id FROM users ORDER BY id DESC"
        assert exec_eq(db, gold, pred_correct)
        assert not exec_eq(db, gold, pred_wrong)

    def test_column_count_mismatch_is_failure(self, db: Path) -> None:
        gold = "SELECT id FROM users WHERE status = 2"
        pred = "SELECT id, name FROM users WHERE status = 2"
        assert not exec_eq(db, gold, pred)

    def test_pred_error_is_failure(self, db: Path) -> None:
        assert not exec_eq(db, "SELECT id FROM users", "broken sql")
