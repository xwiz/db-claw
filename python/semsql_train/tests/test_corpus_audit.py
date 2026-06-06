from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner
from semsql_train.__main__ import cli
from semsql_train.corpus_audit import audit_corpora


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True))
            fh.write("\n")


def _stage2_row(
    question: str,
    skeleton: str,
    *,
    db_id: str = "db",
) -> dict:
    return {
        "stage": 2,
        "db_id": db_id,
        "nl": question,
        "ranked_schema": [{"kind": "entity", "target": "a", "score": 1.0}],
        "slot_map": {"@entity1": "a"},
        "natsql_skeleton": skeleton,
    }


def test_flags_stale_bird_eval_join_coverage(tmp_path: Path) -> None:
    train = tmp_path / "train.jsonl"
    eval_ = tmp_path / "eval.jsonl"
    _write_jsonl(
        train,
        [
            _stage2_row(
                "q1",
                "SELECT @field3 FROM @entity1 INNER JOIN @entity2 ON @field1 = @field2",
            ),
            _stage2_row("q2", "SELECT @field1 FROM @entity1 WHERE @field2 = @val1"),
        ],
    )
    _write_jsonl(
        eval_,
        [
            _stage2_row("q3", "SELECT @field1 FROM @entity1"),
            _stage2_row("q4", "SELECT COUNT(*) FROM @entity1"),
        ],
    )

    report = audit_corpora(
        skeleton_train=train,
        skeleton_eval=eval_,
        profile="bird-stage2",
    )

    assert not report.ok
    assert any("BIRD Stage 2 eval JOIN coverage" in e for e in report.errors)


def test_flags_benchmark_question_overlap(tmp_path: Path) -> None:
    train = tmp_path / "train.jsonl"
    bench = tmp_path / "bird_dev.json"
    _write_jsonl(train, [_stage2_row("How many schools?", "SELECT COUNT(*) FROM @entity1")])
    bench.write_text(
        json.dumps([{"question": "How many schools?", "SQL": "SELECT COUNT(*) FROM schools"}]),
        encoding="utf-8",
    )

    report = audit_corpora(skeleton_train=train, benchmark_questions=bench)

    assert not report.ok
    assert any("overlaps benchmark questions" in e for e in report.errors)


def test_flags_trivial_linker_eval_groups(tmp_path: Path) -> None:
    linker_eval = tmp_path / "linker_eval.jsonl"
    _write_jsonl(
        linker_eval,
        [
            {
                "nl": f"q{i}",
                "candidate_kind": "entity",
                "candidate_target": f"t{i}",
                "relevance_label": 1.0,
            }
            for i in range(10)
        ],
    )

    report = audit_corpora(linker_eval=linker_eval, recall_k=5)

    assert not report.ok
    assert any("cannot validate ranking" in e for e in report.errors)


def test_cli_writes_report_and_exits_nonzero_on_errors(tmp_path: Path) -> None:
    train = tmp_path / "train.jsonl"
    eval_ = tmp_path / "eval.jsonl"
    out = tmp_path / "audit.json"
    _write_jsonl(
        train,
        [
            _stage2_row(
                "q1",
                "SELECT @field3 FROM @entity1 INNER JOIN @entity2 ON @field1 = @field2",
            )
        ],
    )
    _write_jsonl(eval_, [_stage2_row("q2", "SELECT @field1 FROM @entity1")])

    result = CliRunner().invoke(
        cli,
        [
            "audit-corpus",
            "--profile",
            "bird-stage2",
            "--skeleton-train",
            str(train),
            "--skeleton-eval",
            str(eval_),
            "--out",
            str(out),
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["errors"]
