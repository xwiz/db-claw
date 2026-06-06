from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from semsql_eval.binder_probe import DbAtlas
from semsql_eval.queryframe_probe import (
    run_queryframe_probe,
    solve_queryframe,
    solve_queryframe_attempt,
)
from semsql_eval.spider import Example


def _make_superhero_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE publisher (
                id INTEGER PRIMARY KEY,
                publisher_name TEXT
            );
            CREATE TABLE colour (
                id INTEGER PRIMARY KEY,
                colour TEXT
            );
            CREATE TABLE superhero (
                id INTEGER PRIMARY KEY,
                name TEXT,
                height INTEGER,
                eye_colour_id INTEGER,
                publisher_id INTEGER,
                FOREIGN KEY (publisher_id) REFERENCES publisher(id),
                FOREIGN KEY (eye_colour_id) REFERENCES colour(id)
            );
            INSERT INTO publisher VALUES (1, 'Marvel Comics'), (2, 'DC Comics');
            INSERT INTO colour VALUES (1, 'Hazel'), (2, 'Blue');
            INSERT INTO superhero VALUES
                (1, 'Spider-Man', 178, 1, 1),
                (2, 'Wolverine', 160, 2, 1),
                (3, 'Batman', 188, 2, 2);
            """
        )
        conn.commit()
    finally:
        conn.close()


def _make_votes_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE votes (
                id INTEGER PRIMARY KEY,
                CreationDate TEXT
            );
            INSERT INTO votes VALUES
                (1, '2010-01-03'),
                (2, '2010-05-09'),
                (3, '2011-01-03');
            """
        )
        conn.commit()
    finally:
        conn.close()


def test_queryframe_solver_binds_value_to_compatible_joined_field(tmp_path: Path) -> None:
    db_path = tmp_path / "superhero.sqlite"
    _make_superhero_db(db_path)
    atlas = DbAtlas.load("superhero", db_path)
    example = Example(
        db_id="superhero",
        question="What is the average height of superheroes from Marvel Comics?",
        gold_sql=(
            "SELECT AVG(superhero.height) FROM superhero "
            "JOIN publisher ON superhero.publisher_id = publisher.id "
            "WHERE publisher.publisher_name = 'Marvel Comics'"
        ),
        db_path=db_path,
    )

    solved = solve_queryframe(example, atlas, {"candidate_fields": ["superhero.height"]})

    assert solved is not None
    assert "AVG" in solved.sql
    assert '"publisher"."publisher_name" = \'Marvel Comics\'' in solved.sql
    assert "JOIN" in solved.sql


def test_queryframe_solver_reports_not_routed_reason(tmp_path: Path) -> None:
    db_path = tmp_path / "superhero.sqlite"
    _make_superhero_db(db_path)
    atlas = DbAtlas.load("superhero", db_path)
    example = Example(
        db_id="superhero",
        question="List superheroes.",
        gold_sql="SELECT name FROM superhero",
        db_path=db_path,
    )

    attempt = solve_queryframe_attempt(example, atlas, {"candidate_fields": ["superhero.name"]})

    assert attempt.solved is None
    assert attempt.route_reason == "not_routed_no_predicates"


def test_queryframe_solver_projects_fk_display_value(tmp_path: Path) -> None:
    db_path = tmp_path / "superhero.sqlite"
    _make_superhero_db(db_path)
    atlas = DbAtlas.load("superhero", db_path)
    example = Example(
        db_id="superhero",
        question="What is Batman's eye colour?",
        gold_sql=(
            "SELECT colour.colour FROM superhero "
            "JOIN colour ON superhero.eye_colour_id = colour.id "
            "WHERE superhero.name = 'Batman'"
        ),
        db_path=db_path,
    )

    solved = solve_queryframe(
        example,
        atlas,
        {"candidate_fields": ["superhero.eye_colour_id", "superhero.name"]},
    )

    assert solved is not None
    assert '"colour"."colour"' in solved.sql
    assert '"colour"."id" = "superhero"."eye_colour_id"' in solved.sql


def test_queryframe_solver_keeps_id_predicate_on_named_table(tmp_path: Path) -> None:
    db_path = tmp_path / "superhero.sqlite"
    _make_superhero_db(db_path)
    atlas = DbAtlas.load("superhero", db_path)
    example = Example(
        db_id="superhero",
        question="What is the eye colour of superhero with superhero ID 3?",
        gold_sql=(
            "SELECT colour.colour FROM superhero "
            "JOIN colour ON superhero.eye_colour_id = colour.id "
            "WHERE superhero.id = 3"
        ),
        db_path=db_path,
    )

    solved = solve_queryframe(
        example,
        atlas,
        {"candidate_fields": ["superhero.eye_colour_id", "superhero.id"]},
    )

    assert solved is not None
    assert '"superhero"."id" = 3' in solved.sql
    assert '"colour"."id" = 3' not in solved.sql


def test_queryframe_solver_binds_year_to_date_field(tmp_path: Path) -> None:
    db_path = tmp_path / "votes.sqlite"
    _make_votes_db(db_path)
    atlas = DbAtlas.load("codebase_community", db_path)
    example = Example(
        db_id="codebase_community",
        question="How many votes were made in 2010?",
        gold_sql="SELECT COUNT(id) FROM votes WHERE STRFTIME('%Y', CreationDate) = '2010'",
        db_path=db_path,
    )

    solved = solve_queryframe(example, atlas, {"candidate_fields": ["votes.CreationDate"]})

    assert solved is not None
    assert "STRFTIME('%Y', \"votes\".\"CreationDate\") = '2010'" in solved.sql


def test_queryframe_probe_scores_seeded_fixture(tmp_path: Path) -> None:
    db_root = tmp_path / "database"
    db_dir = db_root / "superhero"
    db_dir.mkdir(parents=True)
    db_path = db_dir / "superhero.sqlite"
    _make_superhero_db(db_path)

    questions = tmp_path / "dev.json"
    questions.write_text(
        json.dumps(
            [
                {
                    "db_id": "superhero",
                    "question": "What is the average height of superheroes from Marvel Comics?",
                    "query": (
                        "SELECT AVG(superhero.height) FROM superhero "
                        "JOIN publisher ON superhero.publisher_id = publisher.id "
                        "WHERE publisher.publisher_name = 'Marvel Comics'"
                    ),
                }
            ]
        ),
        encoding="utf-8",
    )
    binder = tmp_path / "binder.json"
    binder.write_text(
        json.dumps(
            {
                "examples": [
                    {
                        "index": 0,
                        "db_id": "superhero",
                        "question": "What is the average height of superheroes from Marvel Comics?",
                        "proof_ready": True,
                        "current_correct": False,
                        "candidate_fields": ["superhero.height", "publisher.publisher_name"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    report = run_queryframe_probe(
        questions_path=questions,
        db_root=db_root,
        suite_name="bird",
        binder_report_json=binder,
    )

    assert report.summary["routed"] == 1
    assert report.summary["correct"] == 1
    assert report.summary["net_recovery"] == 1
    assert report.summary["pred_errors"] == 0
