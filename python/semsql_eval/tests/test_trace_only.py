from __future__ import annotations

from pathlib import Path

import pytest
from semsql_eval.__main__ import _apply_record_to_summary, _score_prediction
from semsql_eval.exec_acc import ExecResult
from semsql_eval.spider import EvalSummary, Example


def test_trace_only_score_skips_sql_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("SQL execution should be skipped")

    monkeypatch.setattr("semsql_eval.__main__.execute", explode)
    example = Example(
        db_id="demo",
        question="q",
        gold_sql="SELECT expensive()",
        db_path=Path("missing.sqlite"),
    )

    record = _score_prediction(
        example,
        "SELECT 1",
        stage_pinned="stage_3",
        repair_attempts=0,
        trace_only=True,
        bail_sentinel="SELECT 0",
    )

    assert record["failure_bucket"] == "not_scored"
    assert record["exec_equal"] is False
    assert record["error"] is None
    assert record["gold_error"] is None


def test_trace_only_keeps_infrastructure_failures_visible() -> None:
    example = Example(
        db_id="demo",
        question="q",
        gold_sql="SELECT 1",
        db_path=Path("missing.sqlite"),
    )

    record = _score_prediction(
        example,
        "SELECT 1",
        stage_pinned="missing_onnx_feature",
        repair_attempts=0,
        trace_only=True,
    )

    assert record["failure_bucket"] == "missing_onnx_feature"
    assert record["bailed"] is True


def test_score_prediction_buckets_sql_execution_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def fake_execute(*_args: object, **_kwargs: object) -> ExecResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ExecResult(rows=((1,),), column_count=1)
        return ExecResult(
            rows=(),
            column_count=0,
            error="sqlite execution timed out",
            timed_out=True,
        )

    monkeypatch.setattr("semsql_eval.__main__.execute", fake_execute)
    example = Example(
        db_id="demo",
        question="q",
        gold_sql="SELECT 1",
        db_path=Path("demo.sqlite"),
    )

    record = _score_prediction(
        example,
        "SELECT slow FROM t",
        stage_pinned="stage_3",
        repair_attempts=0,
        exec_timeout_seconds=0.001,
    )
    summary = EvalSummary(suite="bird")
    _apply_record_to_summary(summary, record)

    assert record["failure_bucket"] == "timeout"
    assert record["timeout"] is True
    assert record["pred_timeout"] is True
    assert record["gold_timeout"] is False
    assert record["timeout_error"] == "sqlite execution timed out"
    assert summary.errored == 0
    assert summary.wrong == 1


def test_score_prediction_keeps_gold_timeout_out_of_gate_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def fake_execute(*_args: object, **_kwargs: object) -> ExecResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ExecResult(
                rows=(),
                column_count=0,
                error="sqlite execution timed out",
                timed_out=True,
            )
        return ExecResult(rows=((1,),), column_count=1)

    monkeypatch.setattr("semsql_eval.__main__.execute", fake_execute)
    example = Example(
        db_id="demo",
        question="q",
        gold_sql="SELECT slow_gold FROM t",
        db_path=Path("demo.sqlite"),
    )

    record = _score_prediction(
        example,
        "SELECT 2",
        stage_pinned="stage_3",
        repair_attempts=0,
        exec_timeout_seconds=0.001,
    )
    summary = EvalSummary(suite="bird")
    _apply_record_to_summary(summary, record)

    assert record["failure_bucket"] == "gold_exec_timeout"
    assert record["timeout"] is False
    assert record["gold_timeout"] is True
    assert record["pred_timeout"] is False
    assert record["gold_error"] == "sqlite execution timed out"
    assert summary.errored == 0
    assert summary.wrong == 1
