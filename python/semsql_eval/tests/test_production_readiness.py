from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner
from semsql_eval.__main__ import cli
from semsql_eval.production_readiness import (
    build_production_readiness_report,
    render_production_readiness_markdown,
)


def test_production_readiness_report_marks_clean_core_as_pilot_safe() -> None:
    report = build_production_readiness_report(
        pathway_report=_pathway_report(route_fail_closed=2),
        queryframe_canary_report=_queryframe_report(),
        llm_safety_report=_llm_safety_report(),
    )

    summary = report["summary"]

    assert summary["readiness_level"] == "pilot_safe"
    assert summary["pilot_safe"] is True
    assert summary["release_candidate"] is False
    assert summary["wrong_accepted_sql"] == 0
    assert summary["fail_closed_route_gaps"] == 2
    assert summary["release_missing"] == [
        "realdb",
        "framework",
        "package_public_smoke",
    ]


def test_production_readiness_report_blocks_wrong_accepted_sql() -> None:
    report = build_production_readiness_report(
        pathway_report=_pathway_report(frame_route_wrong=1),
        queryframe_canary_report=_queryframe_report(),
        llm_safety_report=_llm_safety_report(),
    )

    summary = report["summary"]
    rendered = render_production_readiness_markdown(report)

    assert summary["readiness_level"] == "blocked"
    assert summary["pilot_safe"] is False
    assert summary["wrong_accepted_sql"] == 1
    assert "wrong accepted SQL is `1`; must be `0`" in rendered


def test_production_readiness_counts_queryframe_bails_as_route_gaps() -> None:
    report = build_production_readiness_report(
        pathway_report=_pathway_report(),
        queryframe_canary_report={
            "schema_version": 1,
            "summary": {"pass": False},
            "runs": [
                {
                    "routed_cases": [
                        {"exec_equal": True, "bucket": "correct"},
                        {"exec_equal": False, "bucket": "bailed"},
                    ],
                    "reject_cases": [{"fail_closed": True}],
                }
            ],
        },
        llm_safety_report=_llm_safety_report(),
    )

    assert report["surfaces"]["queryframe_canary"]["status"] == "pass"
    assert report["summary"]["wrong_accepted_sql"] == 0
    assert report["summary"]["fail_closed_route_gaps"] == 1
    assert report["summary"]["pilot_safe"] is True


def test_production_readiness_report_release_requires_public_smoke() -> None:
    report = build_production_readiness_report(
        pathway_report=_pathway_report(),
        queryframe_canary_report=_queryframe_report(),
        llm_safety_report=_llm_safety_report(),
        realdb_reports=[
            _realdb_report(engine="mysql"),
            _realdb_report(engine="postgres", questions=3, analytics=0),
        ],
        framework_reports=[_framework_bridge_report(), _real_app_framework_report()],
        package_public_smoke_report=_package_public_report(),
        require_public_package_smoke=True,
    )

    assert report["summary"]["readiness_level"] == "release_candidate"
    assert report["summary"]["release_candidate"] is True


def test_production_readiness_release_rejects_local_binary_override_smoke() -> None:
    package_smoke = _package_public_report(native_binary_mode="override")

    report = build_production_readiness_report(
        pathway_report=_pathway_report(),
        queryframe_canary_report=_queryframe_report(),
        llm_safety_report=_llm_safety_report(),
        realdb_reports=[
            _realdb_report(engine="mysql"),
            _realdb_report(engine="postgres", questions=3, analytics=0),
        ],
        framework_reports=[_framework_bridge_report(), _real_app_framework_report()],
        package_public_smoke_report=package_smoke,
    )

    assert report["summary"]["readiness_level"] == "blocked"
    assert report["summary"]["release_candidate"] is False
    assert report["surfaces"]["package_public_smoke"]["status"] == "fail"
    assert (
        "native_binary_mode_not_default_release_manifest"
        in report["surfaces"]["package_public_smoke"]["failures"]
    )


def test_production_readiness_release_rejects_dev_package_version() -> None:
    package_smoke = _package_public_report(version="0.1.0-dev")

    report = build_production_readiness_report(
        pathway_report=_pathway_report(),
        queryframe_canary_report=_queryframe_report(),
        llm_safety_report=_llm_safety_report(),
        realdb_reports=[
            _realdb_report(engine="mysql"),
            _realdb_report(engine="postgres", questions=3, analytics=0),
        ],
        framework_reports=[_framework_bridge_report(), _real_app_framework_report()],
        package_public_smoke_report=package_smoke,
    )

    assert report["summary"]["readiness_level"] == "blocked"
    assert report["surfaces"]["package_public_smoke"]["status"] == "fail"
    assert "version_is_dev" in report["surfaces"]["package_public_smoke"]["failures"]


def test_production_readiness_markdown_lists_missing_release_surface_blockers() -> None:
    report = build_production_readiness_report(
        pathway_report=_pathway_report(),
        queryframe_canary_report=_queryframe_report(),
        llm_safety_report=_llm_safety_report(),
    )

    rendered = render_production_readiness_markdown(report)

    assert report["summary"]["readiness_level"] == "pilot_safe"
    assert report["summary"]["release_missing"] == [
        "realdb",
        "framework",
        "package_public_smoke",
    ]
    assert "missing release surface: `realdb`" in rendered
    assert "missing release surface: `framework`" in rendered
    assert "missing release surface: `package_public_smoke`" in rendered


def test_production_readiness_report_blocks_failed_realdb_release_surface() -> None:
    report = build_production_readiness_report(
        pathway_report=_pathway_report(),
        queryframe_canary_report=_queryframe_report(),
        llm_safety_report=_llm_safety_report(),
        realdb_reports=[_realdb_report(status="fail", passed=False)],
        framework_reports=[_framework_bridge_report(), _real_app_framework_report()],
        package_public_smoke_report=_package_public_report(),
    )

    rendered = render_production_readiness_markdown(report)

    assert report["summary"]["readiness_level"] == "blocked"
    assert report["surfaces"]["realdb"]["status"] == "fail"
    assert "release surface failed: `realdb`" in rendered


def test_production_readiness_blocks_shallow_dynamic_evidence() -> None:
    report = build_production_readiness_report(
        pathway_report=_pathway_report(),
        queryframe_canary_report=_queryframe_report(),
        llm_safety_report=_llm_safety_report(),
        realdb_reports=[_realdb_report(questions=1, analytics=0)],
        framework_reports=[_framework_bridge_report(source_vocab_grounded=0)],
        package_public_smoke_report=_package_public_report(),
    )

    assert report["summary"]["readiness_level"] == "blocked"
    assert report["surfaces"]["realdb"]["status"] == "fail"
    assert report["surfaces"]["framework"]["status"] == "fail"
    assert "realdb_question_count_below_minimum" in report["surfaces"]["realdb"]["failures"]
    assert "realdb_engine_count_below_minimum" in report["surfaces"]["realdb"]["failures"]
    assert (
        "framework_source_vocab_grounded_count_below_minimum"
        in report["surfaces"]["framework"]["failures"]
    )


def test_production_readiness_cli_strict_fails_when_core_missing() -> None:
    result = CliRunner().invoke(cli, ["production-readiness-report", "--strict"])

    assert result.exit_code != 0
    assert "production readiness pilot gate failed" in result.output


def test_production_readiness_cli_writes_reports(tmp_path: Path) -> None:
    pathway = _write_json(tmp_path / "pathway.json", _pathway_report())
    canary = _write_json(tmp_path / "canary.json", _queryframe_report())
    safety = _write_json(tmp_path / "safety.json", _llm_safety_report())
    realdb = _write_json(tmp_path / "realdb.json", _realdb_report())
    realdb_extra = _write_json(
        tmp_path / "realdb-extra.json",
        _realdb_report(engine="postgres", questions=3, analytics=0),
    )
    framework = _write_json(tmp_path / "framework.json", _framework_bridge_report())
    framework_extra = _write_json(
        tmp_path / "framework-extra.json",
        _real_app_framework_report(),
    )
    out_json = tmp_path / "readiness.json"
    out_md = tmp_path / "readiness.md"

    result = CliRunner().invoke(
        cli,
        [
            "production-readiness-report",
            "--pathway-report-json",
            str(pathway),
            "--queryframe-canary-json",
            str(canary),
            "--llm-safety-json",
            str(safety),
            "--realdb-json",
            str(realdb),
            "--realdb-json",
            str(realdb_extra),
            "--framework-json",
            str(framework),
            "--framework-json",
            str(framework_extra),
            "--out-json",
            str(out_json),
            "--out-md",
            str(out_md),
            "--strict",
        ],
    )

    assert result.exit_code == 0
    assert "readiness: `pilot_safe`" in result.output
    assert json.loads(out_json.read_text(encoding="utf-8"))["summary"]["pilot_safe"] is True
    assert "SemSQL Production Readiness Report" in out_md.read_text(encoding="utf-8")


def _pathway_report(
    *,
    frame_route_wrong: int = 0,
    route_fail_closed: int = 0,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "benchmark": "pathway-decision-v1",
        "schema_variant": "semantic_alias",
        "summary": {
            "route_total": 10,
            "nonroute_total": 4,
            "policies": {
                "frame_only": {
                    "route_correct": 10 - frame_route_wrong - route_fail_closed,
                    "route_wrong_sql": frame_route_wrong,
                    "route_fail_closed": route_fail_closed,
                    "nonroute_fail_closed": 4,
                    "nonroute_unexpected_sql": 0,
                },
                "bound_plan": {
                    "route_correct": 10 - frame_route_wrong - route_fail_closed,
                    "route_wrong_sql": frame_route_wrong,
                    "route_fail_closed": route_fail_closed,
                    "nonroute_fail_closed": 4,
                    "nonroute_unexpected_sql": 0,
                },
            },
        },
    }


def _queryframe_report() -> dict[str, object]:
    return {
        "schema_version": 1,
        "summary": {
            "pass": True,
            "routed_total": 18,
            "routed_correct": 18,
            "reject_total": 6,
            "reject_fail_closed": 6,
        },
    }


def _llm_safety_report() -> dict[str, object]:
    return {
        "schema_version": 1,
        "pass": True,
        "expected_count": 6,
        "passed_count": 6,
        "failed_count": 0,
        "unexpected_count": 0,
    }


def _package_public_report(
    *,
    native_binary_mode: str = "default_release_manifest",
    version: str = "0.1.0-alpha.1",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "status": "pass",
        "version": version,
        "native_binary_mode": native_binary_mode,
        "checks": {
            "package_versions_ok": True,
            "dlx_version_ok": True,
            "dlx_extract_ok": True,
            "dlx_query_ok": True,
            "dlx_extractor_help_ok": True,
        },
    }


def _realdb_report(
    *,
    status: str = "pass",
    passed: bool = True,
    engine: str = "mysql",
    questions: int = 12,
    analytics: int = 4,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "engine": engine,
        "status": status,
        "summary": {
            "pass": passed,
            "questions": questions,
            "required_questions": min(questions, 8),
            "analytics_questions": analytics,
        },
    }


def _framework_bridge_report(
    *,
    status: str = "pass",
    passed: bool = True,
    source_vocab_grounded: int = 1,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "status": status,
        "summary": {
            "pass": passed,
            "frameworks": 5,
            "passed": 5 if passed else 4,
            "failed": 0 if passed else 1,
            "query_checks": 3,
            "query_ok": 3 if passed else 2,
            "source_vocab_grounded": source_vocab_grounded,
            "source_vocab_dangling": 0,
        },
    }


def _real_app_framework_report() -> dict[str, object]:
    return {
        "schema_version": 1,
        "status": "pass",
        "summary": {
            "pass": True,
            "query_checks": 5,
            "query_checks_ok": 5,
            "source_vocab_grounded": 174,
            "source_vocab_dangling": 0,
        },
    }


def _write_json(path: Path, data: dict[str, object]) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path
