"""Compact production-readiness report over existing SemSQL evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

JsonObject = dict[str, Any]
ReadinessLevel = Literal[
    "blocked",
    "pilot_safe",
    "release_candidate",
    "not_assessed",
]

PRODUCT_POLICIES = ("frame_only", "bound_plan")
MIN_REALDB_EVIDENCE_COUNT = 2
MIN_REALDB_ENGINE_COUNT = 2
MIN_REALDB_QUESTIONS = 10
MIN_REALDB_ANALYTICS_QUESTIONS = 1
MIN_FRAMEWORK_EVIDENCE_COUNT = 2
MIN_FRAMEWORK_COUNT = 5
MIN_FRAMEWORK_QUERY_CHECKS = 3
MIN_FRAMEWORK_SOURCE_VOCAB_GROUNDED = 1
ANALYTICS_EXPECTED_KINDS = frozenset(
    {
        "conditional_rate",
        "grouped_avg",
        "filtered_grouped_avg",
        "value_filtered_grouped_avg",
        "joined_filtered_grouped_avg",
        "multi_joined_filtered_grouped_avg",
    }
)


def load_json_report(path: Path) -> JsonObject:
    parsed = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError(f"expected JSON object in {path}")
    return parsed


def build_production_readiness_report(
    *,
    pathway_report: JsonObject | None = None,
    queryframe_canary_report: JsonObject | None = None,
    llm_safety_report: JsonObject | None = None,
    realdb_reports: list[JsonObject] | None = None,
    framework_reports: list[JsonObject] | None = None,
    package_public_smoke_report: JsonObject | None = None,
    require_public_package_smoke: bool = False,
) -> JsonObject:
    """Summarise whether the current evidence supports promotion.

    This deliberately composes existing reports instead of inventing a new
    benchmark. The policy is conservative: wrong accepted SQL blocks readiness,
    while fail-closed route gaps are tracked as coverage debt.
    """
    surfaces = {
        "pathway": _pathway_surface(pathway_report),
        "queryframe_canary": _queryframe_canary_surface(queryframe_canary_report),
        "llm_safety": _llm_safety_surface(llm_safety_report),
        "realdb": _evidence_set_surface(
            realdb_reports,
            missing_reason="real DB probe JSON not supplied",
            label="realdb",
            minimums={
                "evidence_count": MIN_REALDB_EVIDENCE_COUNT,
                "engine_count": MIN_REALDB_ENGINE_COUNT,
                "question_count": MIN_REALDB_QUESTIONS,
                "analytics_question_count": MIN_REALDB_ANALYTICS_QUESTIONS,
            },
        ),
        "framework": _evidence_set_surface(
            framework_reports,
            missing_reason="framework probe JSON not supplied",
            label="framework",
            minimums={
                "evidence_count": MIN_FRAMEWORK_EVIDENCE_COUNT,
                "framework_count": MIN_FRAMEWORK_COUNT,
                "query_check_count": MIN_FRAMEWORK_QUERY_CHECKS,
                "source_vocab_grounded_count": MIN_FRAMEWORK_SOURCE_VOCAB_GROUNDED,
            },
        ),
        "package_public_smoke": _package_public_surface(package_public_smoke_report),
    }

    core_names = ("pathway", "queryframe_canary")
    release_names = ("llm_safety", "realdb", "framework", "package_public_smoke")

    core_missing = [
        name for name in core_names if surfaces[name]["status"] == "missing"
    ]
    core_failures = [
        name for name in core_names if surfaces[name]["status"] == "fail"
    ]
    release_missing = [
        name for name in release_names if surfaces[name]["status"] == "missing"
    ]
    release_failures = [
        name for name in release_names if surfaces[name]["status"] == "fail"
    ]
    wrong_accepted_sql = sum(
        int(surface.get("wrong_accepted_sql") or 0) for surface in surfaces.values()
    )
    fail_closed_gaps = sum(
        int(surface.get("fail_closed_route_gaps") or 0)
        for surface in surfaces.values()
    )
    core_pass = not core_missing and not core_failures and wrong_accepted_sql == 0
    release_pass = (
        core_pass
        and not release_missing
        and not release_failures
    )

    if core_failures or release_failures or wrong_accepted_sql > 0:
        readiness_level: ReadinessLevel = "blocked"
    elif not core_pass:
        readiness_level = "not_assessed"
    elif release_pass:
        readiness_level = "release_candidate"
    else:
        readiness_level = "pilot_safe"

    return {
        "schema_version": 1,
        "source": "semsql_production_readiness_report",
        "summary": {
            "readiness_level": readiness_level,
            "pilot_safe": core_pass,
            "release_candidate": release_pass,
            "wrong_accepted_sql": wrong_accepted_sql,
            "fail_closed_route_gaps": fail_closed_gaps,
            "core_missing": core_missing,
            "core_failures": core_failures,
            "release_missing": release_missing,
            "release_failures": release_failures,
            "require_public_package_smoke": True,
        },
        "surfaces": surfaces,
        "interpretation": {
            "pilot_safe": (
                "Core product probes have no wrong accepted SQL. Fail-closed "
                "route gaps may remain and should be triaged as coverage debt."
            ),
            "release_candidate": (
                "Core behavior plus configured release/provider safety surfaces "
                "passed. This is still a pre-release gate, not a broad accuracy claim."
            ),
            "blocked": (
                "At least one supplied surface failed, or accepted SQL was wrong."
            ),
            "not_assessed": "Required core evidence is missing.",
        },
    }


def render_production_readiness_markdown(report: JsonObject) -> str:
    summary = report["summary"]
    surfaces = report["surfaces"]
    lines = [
        "# SemSQL Production Readiness Report",
        "",
    ]
    provenance = report.get("provenance")
    if isinstance(provenance, dict):
        generated_at = provenance.get("generated_at_utc")
        eval_version = provenance.get("semsql_eval_version")
        if generated_at:
            lines.append(f"- generated: `{generated_at}`")
        if eval_version:
            lines.append(f"- semsql_eval version: `{eval_version}`")
        if generated_at or eval_version:
            lines.append("")

    lines.extend([
        f"- readiness: `{summary['readiness_level']}`",
        f"- pilot safe: `{summary['pilot_safe']}`",
        f"- release candidate: `{summary['release_candidate']}`",
        f"- wrong accepted SQL: `{summary['wrong_accepted_sql']}`",
        f"- fail-closed route gaps: `{summary['fail_closed_route_gaps']}`",
        "",
        "## Surfaces",
        "",
        "| Surface | Status | Wrong accepted SQL | Route gaps | Notes |",
        "|---|---:|---:|---:|---|",
    ])
    for name in (
        "pathway",
        "queryframe_canary",
        "llm_safety",
        "realdb",
        "framework",
        "package_public_smoke",
    ):
        surface = surfaces[name]
        lines.append(
            "| `{}` | `{}` | `{}` | `{}` | {} |".format(
                name,
                surface["status"],
                surface.get("wrong_accepted_sql", 0),
                surface.get("fail_closed_route_gaps", 0),
                _surface_notes(surface),
            )
        )

    blockers = _blockers(report)
    if blockers:
        lines.extend(["", "## Blockers", ""])
        for blocker in blockers:
            lines.append(f"- {blocker}")

    lines.extend(
        [
            "",
            "## Reading",
            "",
            str(report["interpretation"][summary["readiness_level"]]),
            "",
            "This report is an index over evidence artifacts. It should not replace "
            "`v02-current-status.md` or copy raw run details into living docs.",
        ]
    )
    return "\n".join(lines) + "\n"


def _pathway_surface(report: JsonObject | None) -> JsonObject:
    if report is None:
        return _missing_surface("pathway benchmark JSON not supplied")
    summary = _as_object(report.get("summary"))
    route_total = int(summary.get("route_total") or 0)
    policies = _as_object(summary.get("policies"))
    policy_rows: list[JsonObject] = []
    failures: list[str] = []
    for policy_name in PRODUCT_POLICIES:
        raw_policy = policies.get(policy_name)
        if not isinstance(raw_policy, dict):
            if policy_name == "frame_only":
                failures.append("missing_frame_only_policy")
            continue
        row = _pathway_policy_row(policy_name, raw_policy)
        policy_rows.append(row)
        if int(row["wrong_accepted_sql"]) > 0:
            failures.append(f"{policy_name}_wrong_accepted_sql")

    if route_total <= 0:
        failures.append("no_route_cases")
    primary_policy = _primary_product_policy(policy_rows)
    wrong_total = max(
        (int(row["wrong_accepted_sql"]) for row in policy_rows),
        default=0,
    )
    fail_closed_total = int(primary_policy.get("route_fail_closed") or 0)

    return {
        "status": "fail" if failures else "pass",
        "wrong_accepted_sql": wrong_total,
        "fail_closed_route_gaps": fail_closed_total,
        "route_total": route_total,
        "nonroute_total": int(summary.get("nonroute_total") or 0),
        "schema_variant": report.get("schema_variant"),
        "primary_policy": primary_policy.get("policy"),
        "policies": policy_rows,
        "failures": failures,
    }


def _queryframe_canary_surface(report: JsonObject | None) -> JsonObject:
    if report is None:
        return _missing_surface("queryframe canary JSON not supplied")
    routed_total, routed_correct, wrong, route_gaps = _queryframe_routed_counts(report)
    reject_total, reject_closed, reject_wrong = _queryframe_reject_counts(report)
    failures: list[str] = []
    if routed_total <= 0:
        failures.append("no_routed_cases")
    if wrong > 0:
        failures.append("routed_exec_mismatch")
    if reject_closed != reject_total:
        failures.append("reject_not_fail_closed")
    wrong += reject_wrong
    return {
        "status": "fail" if failures else "pass",
        "wrong_accepted_sql": wrong,
        "fail_closed_route_gaps": route_gaps,
        "routed_total": routed_total,
        "routed_correct": routed_correct,
        "reject_total": reject_total,
        "reject_fail_closed": reject_closed,
        "failures": sorted(set(failures)),
    }


def _queryframe_routed_counts(report: JsonObject) -> tuple[int, int, int, int]:
    rows = _queryframe_rows(report, "routed_cases")
    if not rows:
        summary = _as_object(report.get("summary"))
        routed_total = int(summary.get("routed_total") or 0)
        routed_correct = int(summary.get("routed_correct") or 0)
        return routed_total, routed_correct, max(0, routed_total - routed_correct), 0
    routed_total = len(rows)
    routed_correct = sum(1 for row in rows if row.get("exec_equal") is True)
    wrong = 0
    route_gaps = 0
    for row in rows:
        if row.get("exec_equal") is True:
            continue
        bucket = str(row.get("bucket") or "")
        if bucket in {"bailed", "needs_model", "model_unavailable"}:
            route_gaps += 1
        else:
            wrong += 1
    return routed_total, routed_correct, wrong, route_gaps


def _queryframe_reject_counts(report: JsonObject) -> tuple[int, int, int]:
    rows = _queryframe_rows(report, "reject_cases")
    if not rows:
        summary = _as_object(report.get("summary"))
        reject_total = int(summary.get("reject_total") or 0)
        reject_closed = int(summary.get("reject_fail_closed") or 0)
        return reject_total, reject_closed, max(0, reject_total - reject_closed)
    reject_total = len(rows)
    reject_closed = sum(1 for row in rows if row.get("fail_closed") is True)
    reject_wrong = reject_total - reject_closed
    return reject_total, reject_closed, reject_wrong


def _queryframe_rows(report: JsonObject, key: str) -> list[JsonObject]:
    rows: list[JsonObject] = []
    direct = report.get(key)
    if isinstance(direct, list):
        rows.extend(row for row in direct if isinstance(row, dict))
    runs = report.get("runs")
    if isinstance(runs, list):
        for run in runs:
            if not isinstance(run, dict):
                continue
            run_rows = run.get(key)
            if isinstance(run_rows, list):
                rows.extend(row for row in run_rows if isinstance(row, dict))
    return rows


def _llm_safety_surface(report: JsonObject | None) -> JsonObject:
    if report is None:
        return _missing_surface("LLM safety gate JSON not supplied")
    passed = report.get("pass") is True
    failures: list[str] = [] if passed else ["llm_safety_gate_failed"]
    failed_count = int(report.get("failed_count") or 0)
    unexpected_count = int(report.get("unexpected_count") or 0)
    if failed_count:
        failures.append("failed_cases")
    if unexpected_count:
        failures.append("unexpected_cases")
    return {
        "status": "pass" if passed and not failures else "fail",
        "wrong_accepted_sql": 0,
        "fail_closed_route_gaps": 0,
        "expected_count": int(report.get("expected_count") or 0),
        "passed_count": int(report.get("passed_count") or 0),
        "failed_count": failed_count,
        "unexpected_count": unexpected_count,
        "failures": sorted(set(failures)),
    }


def _evidence_set_surface(
    reports: list[JsonObject] | None,
    *,
    missing_reason: str,
    label: str,
    minimums: dict[str, int],
) -> JsonObject:
    if not reports:
        return _missing_surface(missing_reason)
    rows = [_single_evidence_report_row(report) for report in reports]
    engines = sorted(
        {
            str(row.get("engine"))
            for row in rows
            if isinstance(row.get("engine"), str) and row.get("engine")
        }
    )
    metrics = {
        "report_count": len(rows),
        "evidence_count": sum(max(1, int(row.get("run_total") or 0)) for row in rows),
        "engine_count": len(engines),
        "passed_count": sum(1 for row in rows if row["status"] == "pass"),
        "question_count": sum(int(row.get("questions") or 0) for row in rows),
        "required_question_count": sum(
            int(row.get("required_questions") or 0) for row in rows
        ),
        "analytics_question_count": sum(
            int(row.get("analytics_questions") or 0) for row in rows
        ),
        "framework_count": sum(int(row.get("frameworks") or 0) for row in rows),
        "query_check_count": sum(int(row.get("query_checks") or 0) for row in rows),
        "query_check_ok_count": sum(int(row.get("query_ok") or 0) for row in rows),
        "source_vocab_grounded_count": sum(
            int(row.get("source_vocab_grounded") or 0) for row in rows
        ),
        "source_vocab_dangling_count": sum(
            int(row.get("source_vocab_dangling") or 0) for row in rows
        ),
    }
    failures = [
        f"{label}_report_{index}_not_pass"
        for index, row in enumerate(rows, start=1)
        if row["status"] != "pass" or row["summary_pass"] is False
    ]
    for metric, minimum in minimums.items():
        if int(metrics.get(metric) or 0) < minimum:
            failures.append(f"{label}_{metric}_below_minimum")
    if label == "framework" and metrics["source_vocab_dangling_count"] > 0:
        failures.append("framework_source_vocab_dangling")
    return {
        "status": "fail" if failures else "pass",
        "wrong_accepted_sql": 0,
        "fail_closed_route_gaps": 0,
        **metrics,
        "engines": engines,
        "minimums": minimums,
        "reports": rows,
        "failures": sorted(set(failures)),
    }


def _single_evidence_report_row(report: JsonObject) -> JsonObject:
    summary = _as_object(report.get("summary"))
    status = str(report.get("status") or "unknown")
    summary_pass = summary.get("pass")
    analytics_questions, analytics_source = _analytics_questions_from_summary(summary)
    return {
        "status": status,
        "engine": str(report.get("engine") or ""),
        "summary_pass": summary_pass if isinstance(summary_pass, bool) else None,
        "questions": int(summary.get("questions") or 0),
        "required_questions": int(summary.get("required_questions") or 0),
        "analytics_questions": analytics_questions,
        "analytics_question_source": analytics_source,
        "frameworks": int(summary.get("frameworks") or 0),
        "query_checks": int(summary.get("query_checks") or 0),
        "query_ok": int(
            summary.get("query_ok")
            or summary.get("query_checks_ok")
            or 0
        ),
        "source_vocab_grounded": int(summary.get("source_vocab_grounded") or 0),
        "source_vocab_dangling": int(summary.get("source_vocab_dangling") or 0),
        "run_total": int(summary.get("run_total") or 0),
        "run_passed": int(summary.get("run_passed") or 0),
        "source": report.get("source"),
    }


def _analytics_questions_from_summary(summary: JsonObject) -> tuple[int, str]:
    if "analytics_questions" in summary:
        return int(summary.get("analytics_questions") or 0), "summary.analytics_questions"

    expected_kind_counts = _as_object(summary.get("expected_kind_counts"))
    derived = sum(
        int(expected_kind_counts.get(kind) or 0)
        for kind in ANALYTICS_EXPECTED_KINDS
    )
    if derived:
        return derived, "summary.expected_kind_counts"
    return 0, "none"


def _package_public_surface(report: JsonObject | None) -> JsonObject:
    if report is None:
        return _missing_surface("public package smoke JSON not supplied")
    checks = _as_object(report.get("checks"))
    failed_checks = sorted(str(name) for name, value in checks.items() if value is not True)
    status = report.get("status")
    version = str(report.get("version") or "")
    native_binary_mode = str(report.get("native_binary_mode") or "")
    failures = failed_checks
    if status != "pass":
        failures = ["status_not_pass", *failed_checks]
    if not version:
        failures = [*failures, "version_missing"]
    elif "dev" in version.lower():
        failures = [*failures, "version_is_dev"]
    if native_binary_mode != "default_release_manifest":
        failures = [
            *failures,
            (
                "native_binary_mode_not_default_release_manifest"
                if native_binary_mode
                else "native_binary_mode_missing"
            ),
        ]
    return {
        "status": "fail" if failures else "pass",
        "wrong_accepted_sql": 0,
        "fail_closed_route_gaps": 0,
        "version": version,
        "native_binary_mode": native_binary_mode,
        "failed_checks": failed_checks,
        "failures": sorted(set(failures)),
    }


def _pathway_policy_row(policy_name: str, policy: JsonObject) -> JsonObject:
    route_wrong_sql = int(policy.get("route_wrong_sql") or 0)
    nonroute_unexpected_sql = int(policy.get("nonroute_unexpected_sql") or 0)
    return {
        "policy": policy_name,
        "route_correct": int(policy.get("route_correct") or 0),
        "route_wrong_sql": route_wrong_sql,
        "route_fail_closed": int(policy.get("route_fail_closed") or 0),
        "nonroute_fail_closed": int(policy.get("nonroute_fail_closed") or 0),
        "nonroute_unexpected_sql": nonroute_unexpected_sql,
        "wrong_accepted_sql": route_wrong_sql + nonroute_unexpected_sql,
    }


def _primary_product_policy(policy_rows: list[JsonObject]) -> JsonObject:
    for row in policy_rows:
        if row.get("policy") == "bound_plan":
            return row
    for row in policy_rows:
        if row.get("policy") == "frame_only":
            return row
    return {}


def _missing_surface(reason: str) -> JsonObject:
    return {
        "status": "missing",
        "wrong_accepted_sql": 0,
        "fail_closed_route_gaps": 0,
        "missing_reason": reason,
        "failures": [],
    }


def _surface_notes(surface: JsonObject) -> str:
    if surface["status"] == "missing":
        return str(surface.get("missing_reason") or "missing")
    failures = surface.get("failures")
    if isinstance(failures, list) and failures:
        return ", ".join(f"`{failure}`" for failure in failures)
    if surface.get("status") == "pass":
        return "ok"
    return "see JSON"


def _blockers(report: JsonObject) -> list[str]:
    summary = report["summary"]
    blockers: list[str] = []
    if int(summary["wrong_accepted_sql"]) > 0:
        blockers.append(
            f"wrong accepted SQL is `{summary['wrong_accepted_sql']}`; must be `0`"
        )
    for name in summary["core_missing"]:
        blockers.append(f"missing required core surface: `{name}`")
    for name in summary["core_failures"]:
        blockers.append(f"required core surface failed: `{name}`")
    for name in summary["release_missing"]:
        blockers.append(f"missing release surface: `{name}`")
    for name in summary["release_failures"]:
        blockers.append(f"release surface failed: `{name}`")
    return blockers


def _as_object(value: object) -> JsonObject:
    return value if isinstance(value, dict) else {}
