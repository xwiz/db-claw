"""Distillation tests — preflight only (no torch needed)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from semsql_train.trainers.distill import DistillConfig, preflight


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in records) + "\n",
        encoding="utf-8",
    )


def test_preflight_passes_on_balanced_data(tmp_path: Path) -> None:
    train = tmp_path / "train.jsonl"
    eval_ = tmp_path / "eval.jsonl"
    out = tmp_path / "out"
    _write_jsonl(
        train,
        [
            {"nl": "show users", "candidate_target": "users", "relevance_label": 1.0}
            for _ in range(10)
        ]
        + [
            {"nl": "show users", "candidate_target": f"junk_{i}", "relevance_label": 0.0}
            for i in range(10)
        ],
    )
    _write_jsonl(
        eval_,
        [
            {"nl": "show foo", "candidate_target": "foo", "relevance_label": 1.0}
        ],
    )
    cfg = DistillConfig(train_jsonl=train, eval_jsonl=eval_, output_dir=out)
    ok, issues = preflight(cfg)
    assert ok, issues


def test_preflight_flags_low_positive_fraction(tmp_path: Path) -> None:
    train = tmp_path / "train.jsonl"
    eval_ = tmp_path / "eval.jsonl"
    out = tmp_path / "out"
    _write_jsonl(
        train,
        [
            {"nl": "x", "candidate_target": f"junk_{i}", "relevance_label": 0.0}
            for i in range(100)
        ]
        + [
            {"nl": "x", "candidate_target": "y", "relevance_label": 1.0}
        ],  # ~1% positives → below 5% threshold
    )
    _write_jsonl(
        eval_,
        [{"nl": "x", "candidate_target": "y", "relevance_label": 1.0}],
    )
    cfg = DistillConfig(train_jsonl=train, eval_jsonl=eval_, output_dir=out)
    ok, issues = preflight(cfg)
    assert not ok
    assert any("positive fraction" in i for i in issues)


def test_preflight_rejects_invalid_hyperparameters(tmp_path: Path) -> None:
    train = tmp_path / "train.jsonl"
    eval_ = tmp_path / "eval.jsonl"
    train.touch()
    eval_.touch()
    cfg = DistillConfig(
        train_jsonl=train,
        eval_jsonl=eval_,
        output_dir=tmp_path / "out",
        alpha=1.5,            # invalid
        temperature=0.5,      # invalid (≤ 1.0)
        student_hidden_layers=0,  # invalid
        epochs=0,             # invalid
        batch_size=0,         # invalid
    )
    ok, issues = preflight(cfg)
    assert not ok
    # Each invalid knob surfaces at least one issue.
    msgs = " | ".join(issues)
    assert "alpha" in msgs
    assert "temperature" in msgs
    assert "student_hidden_layers" in msgs


def test_preflight_reports_missing_files(tmp_path: Path) -> None:
    cfg = DistillConfig(
        train_jsonl=tmp_path / "missing_train.jsonl",
        eval_jsonl=tmp_path / "missing_eval.jsonl",
        output_dir=tmp_path / "out",
    )
    ok, issues = preflight(cfg)
    assert not ok
    assert any("missing_train.jsonl" in i for i in issues)
    assert any("missing_eval.jsonl" in i for i in issues)


@pytest.mark.skipif(
    True, reason="full distillation requires torch + a teacher download — skipped in CI"
)
def test_distill_smoke_run(tmp_path: Path) -> None:
    """Live smoke test — skipped by default; flip the skipif to run on a GPU box."""
    from semsql_train.trainers.distill import distill_linker

    train = tmp_path / "train.jsonl"
    eval_ = tmp_path / "eval.jsonl"
    _write_jsonl(
        train,
        [
            {"nl": "show users", "candidate_target": "users", "relevance_label": 1.0},
            {"nl": "show users", "candidate_target": "orders", "relevance_label": 0.0},
        ]
        * 32,
    )
    _write_jsonl(
        eval_,
        [{"nl": "show users", "candidate_target": "users", "relevance_label": 1.0}],
    )
    cfg = DistillConfig(
        train_jsonl=train,
        eval_jsonl=eval_,
        output_dir=tmp_path / "out",
        epochs=1,
        batch_size=4,
        fp16=False,
    )
    report = distill_linker(cfg)
    assert report.student_param_count > 0
