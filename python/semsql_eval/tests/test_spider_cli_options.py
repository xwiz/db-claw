from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner
from semsql_eval.__main__ import cli


def test_spider_cli_oracle_filter_requires_cache(tmp_path: Path) -> None:
    questions = tmp_path / "dev.json"
    questions.write_text("[]", encoding="utf-8")
    db_root = tmp_path / "db"
    db_root.mkdir()

    result = CliRunner().invoke(
        cli,
        [
            "spider",
            "--questions",
            str(questions),
            "--db-root",
            str(db_root),
            "--oracle-covered-only",
        ],
    )

    assert result.exit_code == 2
    assert "--oracle-covered-only requires --oracle-cache" in result.output


def test_spider_cli_applies_oracle_filter_before_offset_and_limit(
    tmp_path: Path, monkeypatch
) -> None:
    questions = tmp_path / "dev.json"
    questions.write_text(
        json.dumps(
            [
                {"db_id": "db", "question": "skip uncovered", "query": "SELECT 1"},
                {"db_id": "db", "question": "covered 1", "query": "SELECT 1"},
                {"db_id": "db", "question": "covered 2", "query": "SELECT 1"},
                {"db_id": "db", "question": "covered 3", "query": "SELECT 1"},
            ]
        ),
        encoding="utf-8",
    )
    db_root = tmp_path / "database"
    (db_root / "db").mkdir(parents=True)
    oracle = tmp_path / "oracle.jsonl"
    oracle.write_text(
        "\n".join(
            json.dumps(
                {
                    "db_id": "db",
                    "nl": question,
                    "natsql_skeleton": "SELECT 1",
                    "ranked_schema": [],
                    "slot_map": {},
                }
            )
            for question in ("covered 1", "covered 2", "covered 3")
        )
        + "\n",
        encoding="utf-8",
    )
    report = tmp_path / "report.json"

    monkeypatch.setattr(
        "semsql_eval.__main__.make_cascade_predictor",
        lambda **_kwargs: (lambda _example: "SELECT 1"),
    )

    result = CliRunner().invoke(
        cli,
        [
            "spider",
            "--questions",
            str(questions),
            "--db-root",
            str(db_root),
            "--oracle-cache",
            str(oracle),
            "--oracle-covered-only",
            "--offset",
            "1",
            "--limit",
            "1",
            "--trace-only",
            "--report-json",
            str(report),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(report.read_text(encoding="utf-8"))
    provenance = payload["metadata"]["provenance"]
    assert provenance["run_started_at_utc"].endswith("Z")
    assert provenance["report_written_at_utc"].endswith("Z")
    assert provenance["semsql_eval_version"] == "0.1.0.dev0"
    assert (
        provenance["semsql_bin_version"]["path"].replace("\\", "/")
        == "target/debug/semsql.exe"
    )
    assert payload["metadata"]["source_total"] == 4
    assert payload["metadata"]["filtered_total"] == 3
    assert payload["metadata"]["offset"] == 1
    assert payload["metadata"]["limit"] == 1
    assert payload["examples"][0]["question"] == "covered 2"


def test_spider_cli_can_require_stage3_traces(tmp_path: Path, monkeypatch) -> None:
    questions = tmp_path / "dev.json"
    questions.write_text(
        json.dumps([{"db_id": "db", "question": "covered", "query": "SELECT 1"}]),
        encoding="utf-8",
    )
    db_root = tmp_path / "database"
    (db_root / "db").mkdir(parents=True)
    oracle = tmp_path / "oracle.jsonl"
    oracle.write_text(
        json.dumps(
            {
                "db_id": "db",
                "nl": "covered",
                "natsql_skeleton": "SELECT @field1 FROM @entity1",
                "ranked_schema": [],
                "slot_map": {},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    report = tmp_path / "report.json"

    monkeypatch.setattr(
        "semsql_eval.__main__.make_cascade_predictor",
        lambda **_kwargs: (lambda _example: "SELECT 1"),
    )

    result = CliRunner().invoke(
        cli,
        [
            "spider",
            "--questions",
            str(questions),
            "--db-root",
            str(db_root),
            "--oracle-cache",
            str(oracle),
            "--oracle-covered-only",
            "--trace-only",
            "--require-stage3-traces",
            "--report-json",
            str(report),
        ],
    )

    assert result.exit_code != 0
    assert "zero Stage 3 slot traces" in result.output
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["metadata"]["require_stage3_traces"] is True
    assert payload["summary"]["stage3_slot_trace_count"] == 0


def test_spider_cli_records_query_telemetry(tmp_path: Path, monkeypatch) -> None:
    questions = tmp_path / "dev.json"
    questions.write_text(
        json.dumps([{"db_id": "db", "question": "covered", "query": "SELECT 1"}]),
        encoding="utf-8",
    )
    db_root = tmp_path / "database"
    (db_root / "db").mkdir(parents=True)
    report = tmp_path / "report.json"

    def fake_make_cascade_predictor(**kwargs):
        on_query_result = kwargs["on_query_result"]
        on_stage = kwargs["on_stage"]

        def predict(example) -> str:
            on_stage(example, "stage_3", 0)
            on_query_result(
                example,
                SimpleNamespace(
                    stage3_slots=[{"slot_name": "@field1", "picked": "db.id"}],
                    query_frame=None,
                    error_detail=None,
                    elapsed_seconds=1.25,
                    stdout_bytes=8,
                    stderr_bytes=34,
                    timed_out_after_seconds=None,
                    stage_timings_us={"stage_1": 100, "stage_3": 250},
                ),
            )
            return "SELECT 1"

        return predict

    monkeypatch.setattr(
        "semsql_eval.__main__.make_cascade_predictor",
        fake_make_cascade_predictor,
    )

    result = CliRunner().invoke(
        cli,
        [
            "spider",
            "--questions",
            str(questions),
            "--db-root",
            str(db_root),
            "--trace-only",
            "--report-json",
            str(report),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(report.read_text(encoding="utf-8"))
    example = payload["examples"][0]
    assert example["query_elapsed_seconds"] == 1.25
    assert example["query_stdout_bytes"] == 8
    assert example["query_stderr_bytes"] == 34
    assert example["query_timed_out_after_seconds"] is None
    assert example["query_stage_timings_us"] == {"stage_1": 100, "stage_3": 250}
    assert payload["summary"]["query_telemetry"]["elapsed_seconds_avg"] == 1.25
    assert payload["summary"]["query_telemetry"]["stderr_bytes_total"] == 34
    assert payload["summary"]["query_telemetry"]["stage_timings_us_total"] == {
        "stage_1": 100,
        "stage_3": 250,
    }


def test_spider_cli_summarizes_runtime_query_frame_routes(
    tmp_path: Path, monkeypatch
) -> None:
    questions = tmp_path / "dev.json"
    questions.write_text(
        json.dumps(
            [
                {"db_id": "db", "question": "routed", "query": "SELECT 1"},
                {"db_id": "db", "question": "fallback", "query": "SELECT 1"},
            ]
        ),
        encoding="utf-8",
    )
    db_root = tmp_path / "database"
    (db_root / "db").mkdir(parents=True)
    report = tmp_path / "report.json"

    def fake_make_cascade_predictor(**kwargs):
        on_query_result = kwargs["on_query_result"]
        on_stage = kwargs["on_stage"]

        def predict(example) -> str:
            routed = example.question == "routed"
            on_stage(example, "stage_0a" if routed else "stage_3", 0)
            on_query_result(
                example,
                SimpleNamespace(
                    stage3_slots=[],
                    query_frame={
                        "schema_version": 3,
                        "runtime_query_frame": {
                            "schema_version": 1,
                            "source": "runtime_graph_query_frame",
                            "question": example.question,
                            "routed": routed,
                            "used_for_final_sql": routed,
                            "route_reason": (
                                "routed" if routed else "not_routed_complex_shape"
                            ),
                            "sql": "SELECT 1" if routed else None,
                            "projection": None,
                            "predicates": [],
                            "required_entities": [],
                            "joins": [],
                        },
                    },
                    error_detail=None,
                    elapsed_seconds=0.1,
                    stdout_bytes=8,
                    stderr_bytes=0,
                    timed_out_after_seconds=None,
                    stage_timings_us={},
                ),
            )
            return "SELECT 1"

        return predict

    monkeypatch.setattr(
        "semsql_eval.__main__.make_cascade_predictor",
        fake_make_cascade_predictor,
    )

    result = CliRunner().invoke(
        cli,
        [
            "spider",
            "--questions",
            str(questions),
            "--db-root",
            str(db_root),
            "--trace-only",
            "--report-json",
            str(report),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["summary"]["runtime_query_frame"] == {
        "count": 2,
        "routed": 1,
        "rejected": 1,
        "used_for_final_sql": 1,
        "routed_not_used": 0,
        "missing": 0,
        "route_reasons": {"not_routed_complex_shape": 1, "routed": 1},
        "routed_failure_buckets": {"not_scored": 1},
        "used_failure_buckets": {"not_scored": 1},
        "rejected_failure_buckets": {"not_scored": 1},
        "rejected_stage_breakdown": {"stage_3": 1},
    }
    assert payload["examples"][0]["runtime_query_frame"]["route_reason"] == "routed"
    assert (
        payload["examples"][1]["runtime_query_frame"]["route_reason"]
        == "not_routed_complex_shape"
    )


def test_spider_cli_filters_by_repeated_db_id_before_offset_and_limit(
    tmp_path: Path, monkeypatch
) -> None:
    questions = tmp_path / "dev.json"
    questions.write_text(
        json.dumps(
            [
                {"db_id": "alpha", "question": "alpha 1", "query": "SELECT 1"},
                {"db_id": "beta", "question": "beta 1", "query": "SELECT 1"},
                {"db_id": "alpha", "question": "alpha 2", "query": "SELECT 1"},
                {"db_id": "gamma", "question": "gamma 1", "query": "SELECT 1"},
            ]
        ),
        encoding="utf-8",
    )
    db_root = tmp_path / "database"
    for db_id in ["alpha", "beta", "gamma"]:
        (db_root / db_id).mkdir(parents=True)
    report = tmp_path / "report.json"

    monkeypatch.setattr(
        "semsql_eval.__main__.make_cascade_predictor",
        lambda **_kwargs: (lambda _example: "SELECT 1"),
    )

    result = CliRunner().invoke(
        cli,
        [
            "spider",
            "--questions",
            str(questions),
            "--db-root",
            str(db_root),
            "--db-id",
            "alpha",
            "--db-id",
            "gamma",
            "--offset",
            "1",
            "--limit",
            "2",
            "--trace-only",
            "--report-json",
            str(report),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["metadata"]["source_total"] == 4
    assert payload["metadata"]["filtered_total"] == 3
    assert payload["metadata"]["db_ids"] == ["alpha", "gamma"]
    assert [ex["question"] for ex in payload["examples"]] == ["alpha 2", "gamma 1"]


def test_spider_cli_filters_by_repeated_source_index_before_other_windowing(
    tmp_path: Path, monkeypatch
) -> None:
    questions = tmp_path / "dev.json"
    questions.write_text(
        json.dumps(
            [
                {"db_id": "alpha", "question": "alpha 0", "query": "SELECT 1"},
                {"db_id": "beta", "question": "beta 1", "query": "SELECT 1"},
                {"db_id": "alpha", "question": "alpha 2", "query": "SELECT 1"},
                {"db_id": "gamma", "question": "gamma 3", "query": "SELECT 1"},
            ]
        ),
        encoding="utf-8",
    )
    db_root = tmp_path / "database"
    for db_id in ["alpha", "beta", "gamma"]:
        (db_root / db_id).mkdir(parents=True)
    report = tmp_path / "report.json"

    monkeypatch.setattr(
        "semsql_eval.__main__.make_cascade_predictor",
        lambda **_kwargs: (lambda _example: "SELECT 1"),
    )

    result = CliRunner().invoke(
        cli,
        [
            "spider",
            "--questions",
            str(questions),
            "--db-root",
            str(db_root),
            "--index",
            "2",
            "--index",
            "3",
            "--db-id",
            "alpha",
            "--trace-only",
            "--report-json",
            str(report),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["metadata"]["source_total"] == 4
    assert payload["metadata"]["filtered_total"] == 1
    assert payload["metadata"]["indexes"] == [2, 3]
    assert [(ex["index"], ex["question"]) for ex in payload["examples"]] == [(2, "alpha 2")]


def test_spider_cli_checkpoints_partial_report_on_failure(
    tmp_path: Path, monkeypatch
) -> None:
    questions = tmp_path / "dev.json"
    questions.write_text(
        json.dumps(
            [
                {"db_id": "alpha", "question": "first", "query": "SELECT 1"},
                {"db_id": "alpha", "question": "second", "query": "SELECT 1"},
            ]
        ),
        encoding="utf-8",
    )
    db_root = tmp_path / "database"
    (db_root / "alpha").mkdir(parents=True)
    report = tmp_path / "report.json"

    def fake_make_cascade_predictor(**_kwargs):
        def predict(example) -> str:
            if example.question == "second":
                raise RuntimeError("boom")
            return "SELECT 1"

        return predict

    monkeypatch.setattr(
        "semsql_eval.__main__.make_cascade_predictor",
        fake_make_cascade_predictor,
    )

    result = CliRunner().invoke(
        cli,
        [
            "spider",
            "--questions",
            str(questions),
            "--db-root",
            str(db_root),
            "--trace-only",
            "--report-json",
            str(report),
        ],
    )

    assert result.exit_code != 0
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["metadata"]["run"]["complete"] is False
    assert payload["metadata"]["run"]["completed"] == 1
    assert payload["metadata"]["run"]["last_completed_index"] == 0
    assert payload["metadata"]["run"]["next_index"] == 1
    assert "RuntimeError: boom" in payload["metadata"]["run"]["interrupted"]
    assert payload["summary"]["total"] == 1
    assert [ex["question"] for ex in payload["examples"]] == ["first"]
