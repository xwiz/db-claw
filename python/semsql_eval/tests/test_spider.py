from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from semsql_eval.spider import Example, SpiderSuite, evaluate


def _make_db(root: Path, db_id: str) -> Path:
    db_dir = root / db_id
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / f"{db_id}.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, status INTEGER);
        INSERT INTO users VALUES (1, 1), (2, 2), (3, 2);
        """
    )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def suite(tmp_path: Path) -> SpiderSuite:
    db_root = tmp_path / "db"
    _make_db(db_root, "demo")
    manifest = tmp_path / "dev.json"
    manifest.write_text(
        json.dumps(
            [
                {
                    "db_id": "demo",
                    "question": "active users",
                    "query": "SELECT id FROM users WHERE status = 2",
                },
                {
                    "db_id": "demo",
                    "question": "all users",
                    "query": "SELECT id FROM users",
                },
            ]
        ),
        encoding="utf-8",
    )
    return SpiderSuite.load(manifest, db_root)


def test_loads_two_examples(suite: SpiderSuite) -> None:
    assert len(suite.examples) == 2


def test_perfect_predictor_scores_one(suite: SpiderSuite) -> None:
    def predict(ex: Example) -> str:
        return ex.gold_sql

    summary = evaluate(suite, predict)
    assert summary.exec_acc == 1.0
    assert summary.errored == 0


def test_wrong_predictor_scores_zero(suite: SpiderSuite) -> None:
    def predict(ex: Example) -> str:
        return "SELECT 0"

    summary = evaluate(suite, predict)
    assert summary.correct == 0


def test_bailed_predictions_bucket_separately_from_wrong(suite: SpiderSuite) -> None:
    # The cascade-bail sentinel "SELECT 1" must NOT credit toward
    # `correct` even when the gold also evaluates to it; instead it
    # falls in the `bailed` bucket so the user can see the Stage 0
    # vs Stage 1+ coverage gap.
    def predict(_ex: Example) -> str:
        return "SELECT 1"

    summary = evaluate(suite, predict)
    assert summary.total == 2
    assert summary.bailed == 2
    assert summary.correct == 0
    assert summary.wrong == 0
    assert summary.bail_rate == 1.0


def test_bailed_normalisation_handles_whitespace_case_semicolon(
    suite: SpiderSuite,
) -> None:
    # The normaliser must absorb common emit-side variations so a
    # change to the binary's stdout shape doesn't silently flip every
    # bail into a `wrong` count.
    def predict(_ex: Example) -> str:
        return "  select   1 ;\n"

    summary = evaluate(suite, predict)
    assert summary.bailed == 2
    assert summary.correct == 0


def test_predictor_exception_counts_as_error(suite: SpiderSuite) -> None:
    def predict(_ex: Example) -> str:
        raise RuntimeError("boom")

    captured: list[str] = []
    summary = evaluate(suite, predict, on_error=lambda _e, exc: captured.append(str(exc)))
    assert summary.errored == 2
    assert summary.correct == 0
    assert captured == ["boom", "boom"]


def test_bird_field_alias(tmp_path: Path) -> None:
    db_root = tmp_path / "db"
    _make_db(db_root, "demo")
    manifest = tmp_path / "bird_dev.json"
    manifest.write_text(
        json.dumps(
            [
                {
                    "db_id": "demo",
                    "question": "all users",
                    "SQL": "SELECT id FROM users",
                }
            ]
        ),
        encoding="utf-8",
    )
    s = SpiderSuite.load(manifest, db_root, name="bird")
    assert s.examples[0].gold_sql == "SELECT id FROM users"
