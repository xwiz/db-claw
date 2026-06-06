from __future__ import annotations

from pathlib import Path

from semsql_eval.semantic_atlas_assessment import (
    render_semantic_atlas_assessment_markdown,
    run_semantic_atlas_assessment,
)


def test_semantic_atlas_assessment_shows_practical_lift(tmp_path: Path) -> None:
    report = run_semantic_atlas_assessment(out_dir=tmp_path)
    summary = report["summary"]

    assert summary["route_total"] == 31
    assert summary["nonroute_total"] == 13
    assert (
        summary["modes"]["semantic"]["route_plan_ready"]
        > summary["modes"]["raw"]["route_plan_ready"]
    )
    assert summary["modes"]["semantic"]["nonroute_fail_closed"] == 13
    assert summary["modes"]["semantic"]["wrong_accept_risk"] == 0
    assert summary["semantic_lift"]["route_plan_ready_delta"] > 0


def test_semantic_atlas_assessment_can_run_one_suite(tmp_path: Path) -> None:
    report = run_semantic_atlas_assessment(out_dir=tmp_path, suites=("platform",))

    assert report["summary"]["route_total"] == 11
    assert report["summary"]["nonroute_total"] == 7
    assert report["suites"][0]["suite"] == "platform"


def test_semantic_atlas_assessment_markdown_has_readout(tmp_path: Path) -> None:
    report = run_semantic_atlas_assessment(out_dir=tmp_path)
    rendered = render_semantic_atlas_assessment_markdown(report)

    assert "Mini SemanticAtlas Practical Assessment" in rendered
    assert "`raw`" in rendered
    assert "`semantic`" in rendered
    assert "Remaining Semantic Gaps" in rendered
