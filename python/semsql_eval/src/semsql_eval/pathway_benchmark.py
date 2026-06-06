"""Decision benchmark for Stage 3 / Stage 4 architecture choices.

The benchmark reuses the practical platform suites and scores the same runtime
trace under a few acceptance policies. This makes the architectural trade-off
visible without needing to maintain separate runtime forks.
"""

from __future__ import annotations

import random
import shutil
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Literal

from .cascade_runner import (
    CascadeQueryResult,
    build_graph_for_db,
    run_cascade_query,
)
from .exec_acc import exec_results_eq, execute
from .platform_suite import (
    SchemaVariant,
    build_business_analytics_suite,
    build_platform_query_suite,
)

SuiteChoice = Literal["platform", "business"]

POLICIES = ("current_permissive", "frame_only", "bounded_stage3", "bound_plan")
PRODUCT_GATE_POLICIES = ("frame_only", "bound_plan")


def run_pathway_benchmark(
    *,
    out_dir: Path,
    semsql_bin: Path,
    suites: tuple[SuiteChoice, ...] = ("platform", "business"),
    graph_cache_dir: Path | None = None,
    cascade_manifest: Path | None = None,
    intent_yaml: Path | None = None,
    paraphrase_variants_per_route: int = 0,
    paraphrase_seed: int = 17,
    schema_variant: SchemaVariant = "canonical",
    schema_alias_seed: int = 20260605,
    query_timeout_seconds: int = 30,
    extract_timeout_seconds: int = 60,
    exec_timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Run the decision benchmark and return a JSON-serialisable report."""
    if not semsql_bin.exists():
        found = shutil.which("semsql")
        if found is None:
            raise FileNotFoundError(
                f"semsql binary not found at {semsql_bin} and not on PATH"
            )
        semsql_bin = Path(found)
    out_dir.mkdir(parents=True, exist_ok=True)
    graph_root = graph_cache_dir or (out_dir / "graphs")
    frame_root = out_dir / "frames"
    if graph_cache_dir is None and graph_root.exists():
        shutil.rmtree(graph_root)
    if frame_root.exists():
        shutil.rmtree(frame_root)
    graph_root.mkdir(parents=True, exist_ok=True)
    frame_root.mkdir(parents=True, exist_ok=True)

    case_rows: list[dict[str, Any]] = []
    suite_reports: list[dict[str, Any]] = []
    for suite_index, suite_name in enumerate(suites):
        suite_out = out_dir / suite_name
        suite = (
            build_platform_query_suite(
                suite_out,
                schema_variant=schema_variant,
                schema_alias_seed=schema_alias_seed,
            )
            if suite_name == "platform"
            else build_business_analytics_suite(
                suite_out,
                schema_variant=schema_variant,
                schema_alias_seed=schema_alias_seed,
            )
        )
        if paraphrase_variants_per_route > 0:
            suite = _suite_with_route_paraphrases(
                suite,
                variants_per_route=paraphrase_variants_per_route,
                seed=paraphrase_seed + (suite_index * 1009),
            )
        suite_rows = _run_suite_cases(
            suite_name=suite_name,
            suite=suite,
            semsql_bin=semsql_bin,
            graph_root=graph_root,
            frame_root=frame_root / suite_name,
            cascade_manifest=cascade_manifest,
            intent_yaml=intent_yaml,
            query_timeout_seconds=query_timeout_seconds,
            extract_timeout_seconds=extract_timeout_seconds,
            exec_timeout_seconds=exec_timeout_seconds,
        )
        case_rows.extend(suite_rows)
        suite_reports.append(
            {
                "suite": suite_name,
                "db_id": suite["db_id"],
                "schema_variant": suite.get("schema_variant", "canonical"),
                "cases": len(suite_rows),
                "path": str(suite_out),
            }
        )

    return {
        "schema_version": 1,
        "benchmark": "pathway-decision-v1",
        "out_dir": str(out_dir),
        "semsql_bin": str(semsql_bin),
        "cascade_manifest": str(cascade_manifest) if cascade_manifest else None,
        "intent_yaml": str(intent_yaml) if intent_yaml else None,
        "schema_variant": schema_variant,
        "schema_alias_seed": schema_alias_seed if schema_variant == "random_alias" else None,
        "paraphrase_variants_per_route": paraphrase_variants_per_route,
        "paraphrase_seed": paraphrase_seed,
        "suites": suite_reports,
        "summary": _summarise(
            case_rows,
            paraphrase_variants_per_route=paraphrase_variants_per_route,
        ),
        "variant_coverage": _variant_coverage(
            case_rows,
            variants_per_route=paraphrase_variants_per_route,
        ),
        "cases": case_rows,
    }


def render_pathway_benchmark_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    policy = summary["policies"]
    lines = [
        "# Pathway Decision Benchmark",
        "",
        f"- benchmark: `{report['benchmark']}`",
        f"- cases: `{summary['cases_total']}`",
        f"- route cases: `{summary['route_total']}`",
        f"- non-route cases: `{summary['nonroute_total']}`",
        f"- semsql bin: `{report['semsql_bin']}`",
        f"- schema variant: `{report.get('schema_variant', 'canonical')}`",
        f"- paraphrase variants per route: `{report.get('paraphrase_variants_per_route', 0)}`",
        "",
        "## Policy Outcomes",
        "",
        "| policy | route correct | route wrong SQL | route fail-closed | non-route fail-closed | non-route unexpected SQL | read |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    if report.get("schema_variant") == "random_alias":
        lines.insert(8, f"- schema alias seed: `{report.get('schema_alias_seed')}`")
    reads = {
        "current_permissive": "Current behavior: maximizes emitted SQL and exposes wrong-SQL risk.",
        "frame_only": "Current promoted-frame proxy. Any wrong SQL here is a frame legality/promotion bug, not a Stage 3 bug.",
        "bounded_stage3": "Transition proxy. Accepts promoted frames plus non-escalated Stage 3 with a pre-Stage-3 contract.",
        "bound_plan": "Typed compiler-boundary proxy. Accepts only SQL backed by a valid BoundQueryPlan packet.",
    }
    for name in POLICIES:
        row = policy.get(name)
        if row is None:
            continue
        lines.append(
            "| `{}` | `{}/{}` | `{}` | `{}` | `{}/{}` | `{}` | {} |".format(
                name,
                row["route_correct"],
                summary["route_total"],
                row["route_wrong_sql"],
                row["route_fail_closed"],
                row["nonroute_fail_closed"],
                summary["nonroute_total"],
                row["nonroute_unexpected_sql"],
                reads[name],
            )
        )

    lines.extend(
        [
        "",
        "## Runtime Signals",
        "",
        "| signal | count |",
        "|---|---:|",
    ]
    )
    for key, value in summary["signals"].items():
        lines.append(f"| `{key}` | `{value}` |")

    _append_variant_coverage(lines, report.get("variant_coverage"))

    lines.extend(
        [
            "",
            "## Pathway Read",
            "",
            "| pathway | practical outcome on this benchmark | decision use |",
            "|---|---|---|",
            "| Current permissive Stage 3 + repairing Stage 4 | Fastest way to get SQL, but every `route_wrong_sql` or `nonroute_unexpected_sql` is product risk. | Keep only as diagnostic/backward-compat mode. |",
            "| Current promoted-frame only | Shows whether today's QueryFrame/state-machine promotion is already precise enough. Wrong SQL here means the typed compiler boundary still needs legality checks. | Use as the product safety regression bar. |",
            "| Bounded Stage 3 ranker | Measures whether Stage 3 is safe when constrained to candidate-backed, non-escalated choices. Wrong SQL here means candidate presence is not enough. | Use only if wrong-SQL count is near zero. |",
            "| BoundQueryPlan gate | Measures the new typed plan boundary separately from older route-local safety checks. Wrong SQL here means the bound plan is structurally valid but semantically incomplete. | Use as the main transition stoplight for the SemanticAtlas implementation. |",
            "| Direct LLM or agent fallback | Not executed here; rows that fail closed under frame-only are the candidate packet set for typed LLM proposals. | Add after the validator exists, not before. |",
            "",
            "## Current Risk Backlog",
            "",
        ]
    )
    _append_backlog(lines, summary["current_wrong_by_family"], "Current route wrong SQL by family")
    _append_backlog(lines, summary["current_unexpected_by_family"], "Current non-route unexpected SQL by family")
    _append_backlog(lines, summary["frame_only_false_negative_by_family"], "Frame-only route false negatives by family")

    lines.extend(
        [
            "",
            "## Case Matrix",
            "",
            "| suite | id | disposition | family | stage | current | frame-only | bounded-stage3 | bound-plan |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
    )
    for case in report["cases"]:
        lines.append(
            "| `{suite}` | `{case_id}` | `{disposition}` | `{family}` | `{stage}` | `{current}` | `{frame}` | `{bounded}` | `{bound}` |".format(
                suite=case["suite"],
                case_id=case["id"],
                disposition=case["disposition"],
                family=case["family"],
                stage=case["stage_pinned"],
                current=case["policies"]["current_permissive"]["bucket"],
                frame=case["policies"]["frame_only"]["bucket"],
                bounded=case["policies"]["bounded_stage3"]["bucket"],
                bound=case["policies"].get("bound_plan", {}).get("bucket", "n/a"),
            )
        )
    lines.append("")
    return "\n".join(lines)


def pathway_product_gate_failures(report: dict[str, Any]) -> list[str]:
    """Return release-blocking pathway failures.

    Route gaps are allowed to fail closed; accepted wrong SQL is not.
    """
    summary = report.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    route_total = int(summary.get("route_total") or 0)
    nonroute_total = int(summary.get("nonroute_total") or 0)
    policies = summary.get("policies")
    policies = policies if isinstance(policies, dict) else {}

    failures: list[str] = []
    if route_total <= 0:
        failures.append("no_route_cases")
    if nonroute_total <= 0:
        failures.append("no_nonroute_cases")

    for policy_name in PRODUCT_GATE_POLICIES:
        policy = policies.get(policy_name)
        if not isinstance(policy, dict):
            failures.append(f"missing_{policy_name}_policy")
            continue
        route_wrong_sql = int(policy.get("route_wrong_sql") or 0)
        nonroute_unexpected_sql = int(policy.get("nonroute_unexpected_sql") or 0)
        if route_wrong_sql > 0:
            failures.append(f"{policy_name}_route_wrong_sql")
        if nonroute_unexpected_sql > 0:
            failures.append(f"{policy_name}_nonroute_unexpected_sql")

    return failures


def _suite_with_route_paraphrases(
    suite: dict[str, Any], *, variants_per_route: int, seed: int
) -> dict[str, Any]:
    cases = suite.get("cases")
    if not isinstance(cases, list) or variants_per_route <= 0:
        return suite

    rng = random.Random(seed)
    expanded: list[dict[str, Any]] = []
    for raw in cases:
        if not isinstance(raw, dict):
            continue
        expanded.append(raw)
        if raw.get("disposition") != "route":
            continue
        variants = _route_case_paraphrases(raw, rng=rng)
        for offset, question in enumerate(variants[:variants_per_route], start=1):
            if question == raw.get("question"):
                continue
            clone = dict(raw)
            clone["id"] = f"{raw.get('id', 'case')}-p{offset}"
            clone["question"] = question
            clone["variant_of"] = raw.get("id")
            clone["variant_kind"] = "seeded_paraphrase"
            expanded.append(clone)

    out = dict(suite)
    out["cases"] = expanded
    out["paraphrase_seed"] = seed
    out["paraphrase_variants_per_route"] = variants_per_route
    return out


def _route_case_paraphrases(case: dict[str, Any], *, rng: random.Random) -> list[str]:
    question = str(case.get("question", ""))
    family = str(case.get("family", ""))
    candidates: list[str] = []

    if question.startswith("List "):
        candidates.append("Show " + question.removeprefix("List "))
        candidates.append("Show me " + question.removeprefix("List "))
        candidates.append("Display " + question.removeprefix("List "))
    if question.startswith("Show "):
        candidates.append("List " + question.removeprefix("Show "))
        candidates.append("Display " + question.removeprefix("Show "))
        candidates.append("Show me " + question.removeprefix("Show "))
    if question.startswith("Which "):
        rest = question.removeprefix("Which ")
        if " resolved " in rest:
            subject, tail = rest.split(" resolved ", 1)
            candidates.append(f"Show the {subject} who resolved {tail}")
            candidates.append(f"List the {subject} who resolved {tail}")
        elif rest.startswith(("active ", "open ", "converted ", "resolved ")):
            candidates.append("Show " + rest)
            candidates.append("List " + rest)
    if question.startswith("How many "):
        candidates.append("Count " + question.removeprefix("How many "))
        candidates.append("Show count of " + question.removeprefix("How many "))
        candidates.append("Display count of " + question.removeprefix("How many "))
    if question.startswith("Total "):
        candidates.append("Sum " + question.removeprefix("Total "))
        candidates.append("Show total " + question.removeprefix("Total "))
        candidates.append("Display total " + question.removeprefix("Total "))
    if question.startswith("Average "):
        candidates.append("Avg " + question.removeprefix("Average "))
        candidates.append("Show average " + question.removeprefix("Average "))
    if question.startswith("Top "):
        candidates.append("Show top " + question.removeprefix("Top "))
        candidates.append("List top " + question.removeprefix("Top "))
        candidates.append("Display top " + question.removeprefix("Top "))
    if question.startswith("Find "):
        candidates.append("Lookup " + question.removeprefix("Find "))
        candidates.append("Show " + question.removeprefix("Find "))
        candidates.append("Display " + question.removeprefix("Find "))

    if not question.startswith(
        (
            "Average ",
            "Compare ",
            "Count ",
            "Display ",
            "Find ",
            "How many ",
            "List ",
            "Lookup ",
            "Show ",
            "Sum ",
            "Top ",
            "Total ",
            "Which ",
        )
    ):
        if " with " in question or " by " in question or " owned by " in question:
            candidates.append("Show " + question)
            candidates.append("List " + question)
            candidates.append("Display " + question)
        if " by " in question:
            candidates.append("Break down " + question)

    replacements = [
        (" with their ", " including their "),
        (" have no ", " without "),
        (" have not had ", " without any "),
        (" came from ", " originated from "),
        (" by source channel ", " grouped by source channel "),
        (" by support agent", " grouped by support agent"),
        (" by segment", " grouped by segment"),
        (" by campaign", " grouped by campaign"),
        (" by industry", " grouped by industry"),
        (" by region", " grouped by region"),
        (" by stage", " grouped by stage"),
    ]
    for old, new in replacements:
        if old in question:
            candidates.append(question.replace(old, new, 1))

    if family == "anti_join_temporal":
        candidates.extend(
            [
                question.replace("have no ", "without ", 1),
                question.replace("have not had ", "without any ", 1),
            ]
        )
    if family in {"grouped_metric_comparison", "ratio_by_group", "ratio_by_joined_dimension"}:
        candidates.append(question.replace("Compare ", "Show ", 1))
        candidates.append(question.replace("Compare ", "Display ", 1))
    if family.endswith("_group") or "group" in family or "by_" in family:
        candidates.append("Break down " + question)
    if family in {"date_range_count", "growth_channel_count"}:
        candidates.append(question.replace("How many ", "Count ", 1))

    deduped: list[str] = []
    seen = {question}
    for candidate in candidates:
        candidate = " ".join(candidate.split())
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        deduped.append(candidate)
    rng.shuffle(deduped)
    return deduped


def _run_suite_cases(
    *,
    suite_name: SuiteChoice,
    suite: dict[str, Any],
    semsql_bin: Path,
    graph_root: Path,
    frame_root: Path,
    cascade_manifest: Path | None,
    intent_yaml: Path | None,
    query_timeout_seconds: int,
    extract_timeout_seconds: int,
    exec_timeout_seconds: float,
) -> list[dict[str, Any]]:
    db_id = str(suite["db_id"])
    sqlite_path = Path(str(suite["sqlite_path"]))
    graph_path = graph_root / f"{db_id}.semsql"
    vocab_jsonl = Path(str(suite["vocab_jsonl"])) if suite.get("vocab_jsonl") else None
    schema_description_dir = (
        Path(str(suite["schema_description_dir"]))
        if suite.get("schema_description_dir")
        else None
    )
    build_graph_for_db(
        semsql_bin,
        sqlite_path,
        graph_path,
        timeout_seconds=extract_timeout_seconds,
        vocab_jsonl=vocab_jsonl,
        schema_description_dir=schema_description_dir,
    )
    cases = suite["cases"]
    if not isinstance(cases, list):
        return []
    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(cases):
        if not isinstance(raw, dict):
            continue
        frame_path = frame_root / f"{index:03d}-{raw.get('id', 'case')}.json"
        result = run_cascade_query(
            semsql_bin,
            graph_path,
            str(raw["question"]),
            timeout_seconds=query_timeout_seconds,
            cascade_manifest=cascade_manifest,
            intent_yaml=intent_yaml,
            query_frame_json=frame_path,
        )
        rows.append(
            _case_row(
                suite_name=suite_name,
                case=raw,
                result=result,
                sqlite_path=sqlite_path,
                frame_path=frame_path,
                exec_timeout_seconds=exec_timeout_seconds,
            )
        )
    return rows


def _case_row(
    *,
    suite_name: SuiteChoice,
    case: dict[str, Any],
    result: CascadeQueryResult,
    sqlite_path: Path,
    frame_path: Path,
    exec_timeout_seconds: float,
) -> dict[str, Any]:
    disposition = str(case["disposition"])
    is_route = disposition == "route"
    gold_sql = case.get("expected_sql")
    gold_error = None
    pred_error = None
    exec_equal: bool | None = None
    if isinstance(gold_sql, str) and result.sql is not None:
        gold = execute(sqlite_path, gold_sql, timeout_seconds=exec_timeout_seconds)
        pred = execute(sqlite_path, result.sql, timeout_seconds=exec_timeout_seconds)
        exec_equal = exec_results_eq(gold_sql, gold, pred)
        gold_error = gold.error
        pred_error = pred.error

    features = _result_features(result)
    policies = {
        name: _score_policy(
            policy=name,
            result=result,
            is_route=is_route,
            exec_equal=exec_equal,
            features=features,
        )
        for name in POLICIES
    }
    return {
        "suite": suite_name,
        "id": str(case["id"]),
        "question": str(case["question"]),
        "disposition": disposition,
        "family": str(case["family"]),
        "difficulty": str(case["difficulty"]),
        "variant_of": case.get("variant_of"),
        "variant_kind": case.get("variant_kind", "base"),
        "expected_sql": gold_sql,
        "pred_sql": result.sql,
        "exec_equal": exec_equal,
        "gold_error": gold_error,
        "pred_error": pred_error,
        "stage_pinned": result.stage_pinned,
        "error_detail": result.error_detail,
        "elapsed_seconds": result.elapsed_seconds,
        "stage_timings_us": result.stage_timings_us,
        "query_frame_path": str(frame_path) if frame_path.exists() else None,
        "features": features,
        "policies": policies,
    }


def _result_features(result: CascadeQueryResult) -> dict[str, Any]:
    frame = result.query_frame if isinstance(result.query_frame, dict) else {}
    runtime = frame.get("runtime_query_frame") if isinstance(frame, dict) else None
    runtime = runtime if isinstance(runtime, dict) else {}
    bound_plan = frame.get("bound_query_plan") if isinstance(frame, dict) else None
    bound_plan = bound_plan if isinstance(bound_plan, dict) else {}
    stage3 = frame.get("stage3") if isinstance(frame, dict) else None
    slots = []
    if isinstance(stage3, dict) and isinstance(stage3.get("slots"), list):
        slots = [slot for slot in stage3["slots"] if isinstance(slot, dict)]
    elif result.stage3_slots:
        slots = result.stage3_slots
    escalated = sum(1 for slot in slots if slot.get("escalated") is True)
    runtime_used = runtime.get("used_for_final_sql") is True
    stage0_sql = result.sql is not None and result.stage_pinned == "stage_0a"
    return {
        "frame_promoted": bool(runtime_used or stage0_sql),
        "runtime_routed": runtime.get("routed") is True,
        "runtime_used_for_final_sql": runtime_used,
        "runtime_route_reason": runtime.get("route_reason"),
        "bound_plan_valid": bound_plan.get("valid") is True,
        "bound_plan_reject_reason": bound_plan.get("reject_reason"),
        "bound_plan_has_sql": isinstance(bound_plan.get("sql"), str),
        "bound_plan_diagnostic_count": len(bound_plan.get("diagnostics") or []),
        "has_pre_stage3": frame.get("pre_stage3") is not None,
        "stage3_slot_count": len(slots),
        "stage3_escalated_count": escalated,
        "stage3_non_escalated": result.stage_pinned == "stage_3" and escalated == 0,
    }


def _score_policy(
    *,
    policy: str,
    result: CascadeQueryResult,
    is_route: bool,
    exec_equal: bool | None,
    features: dict[str, Any],
) -> dict[str, Any]:
    accept = _policy_accepts(policy, result, features)
    if is_route:
        if not accept:
            bucket = "fail_closed"
        elif exec_equal is True:
            bucket = "correct"
        elif result.sql is None:
            bucket = "fail_closed"
        else:
            bucket = "wrong_sql"
    else:
        bucket = "unexpected_sql" if accept else "fail_closed"
    return {"accept": accept, "bucket": bucket}


def _policy_accepts(
    policy: str,
    result: CascadeQueryResult,
    features: dict[str, Any],
) -> bool:
    if result.sql is None:
        return False
    if policy == "current_permissive":
        return True
    if policy == "frame_only":
        return bool(features["frame_promoted"])
    if policy == "bounded_stage3":
        return bool(
            features["frame_promoted"]
            or (
                result.stage_pinned == "stage_3"
                and features["has_pre_stage3"]
                and features["stage3_escalated_count"] == 0
            )
        )
    if policy == "bound_plan":
        return bool(features["bound_plan_valid"] and features["bound_plan_has_sql"])
    raise ValueError(f"unknown policy {policy!r}")


def _summarise(
    cases: list[dict[str, Any]], *, paraphrase_variants_per_route: int = 0
) -> dict[str, Any]:
    route_total = sum(1 for case in cases if case["disposition"] == "route")
    nonroute_total = len(cases) - route_total
    policies: dict[str, dict[str, int]] = {}
    for policy in POLICIES:
        policies[policy] = {
            "route_correct": _count_policy(cases, policy, "route", "correct"),
            "route_wrong_sql": _count_policy(cases, policy, "route", "wrong_sql"),
            "route_fail_closed": _count_policy(cases, policy, "route", "fail_closed"),
            "nonroute_fail_closed": _count_policy(cases, policy, "nonroute", "fail_closed"),
            "nonroute_unexpected_sql": _count_policy(
                cases, policy, "nonroute", "unexpected_sql"
            ),
        }

    stage_counts = Counter(str(case["stage_pinned"]) for case in cases)
    variant_cases = [case for case in cases if case.get("variant_kind") != "base"]
    base_route_cases = [
        case
        for case in cases
        if case["disposition"] == "route" and case.get("variant_kind") == "base"
    ]
    requested_route_variants = len(base_route_cases) * paraphrase_variants_per_route
    signals = {
        "base_cases": len(cases) - len(variant_cases),
        "base_route_cases": len(base_route_cases),
        "variant_cases": len(variant_cases),
        "variant_route_cases": sum(
            1 for case in variant_cases if case["disposition"] == "route"
        ),
        "requested_route_variant_cases": requested_route_variants,
        "variant_bound_plan_route_correct": sum(
            1
            for case in variant_cases
            if case["disposition"] == "route"
            and case["policies"]["bound_plan"]["bucket"] == "correct"
        ),
        "variant_bound_plan_route_wrong_sql": sum(
            1
            for case in variant_cases
            if case["disposition"] == "route"
            and case["policies"]["bound_plan"]["bucket"] == "wrong_sql"
        ),
        "variant_bound_plan_route_fail_closed": sum(
            1
            for case in variant_cases
            if case["disposition"] == "route"
            and case["policies"]["bound_plan"]["bucket"] == "fail_closed"
        ),
        "stage3_sql_total": sum(
            1 for case in cases if case["stage_pinned"] == "stage_3" and case["pred_sql"]
        ),
        "stage3_route_wrong_sql": sum(
            1
            for case in cases
            if case["disposition"] == "route"
            and case["stage_pinned"] == "stage_3"
            and case["pred_sql"]
            and case["exec_equal"] is not True
        ),
        "stage3_nonroute_unexpected_sql": sum(
            1
            for case in cases
            if case["disposition"] != "route"
            and case["stage_pinned"] == "stage_3"
            and case["pred_sql"]
        ),
        "stage3_escalated_slots": sum(
            int(case["features"]["stage3_escalated_count"]) for case in cases
        ),
        "stage3_sql_with_escalation": sum(
            1
            for case in cases
            if case["stage_pinned"] == "stage_3"
            and case["pred_sql"]
            and int(case["features"]["stage3_escalated_count"]) > 0
        ),
        "runtime_routed": sum(1 for case in cases if case["features"]["runtime_routed"]),
        "runtime_used_for_final_sql": sum(
            1 for case in cases if case["features"]["runtime_used_for_final_sql"]
        ),
        "runtime_routed_not_promoted": sum(
            1
            for case in cases
            if case["features"]["runtime_routed"]
            and not case["features"]["runtime_used_for_final_sql"]
        ),
        "frame_promoted": sum(1 for case in cases if case["features"]["frame_promoted"]),
        "frame_promoted_route_wrong_sql": sum(
            1
            for case in cases
            if case["disposition"] == "route"
            and case["features"]["frame_promoted"]
            and case["pred_sql"]
            and case["exec_equal"] is not True
        ),
        "bounded_stage3_accepts_stage3_sql": sum(
            1
            for case in cases
            if case["stage_pinned"] == "stage_3"
            and case["policies"]["bounded_stage3"]["accept"]
        ),
        "bound_plan_valid": sum(1 for case in cases if case["features"]["bound_plan_valid"]),
        "bound_plan_missing": sum(
            1
            for case in cases
            if case["pred_sql"] and not case["features"]["bound_plan_has_sql"]
        ),
        "bound_plan_diagnostics": sum(
            int(case["features"]["bound_plan_diagnostic_count"]) for case in cases
        ),
        "bound_plan_route_wrong_sql": sum(
            1
            for case in cases
            if case["disposition"] == "route"
            and case["policies"]["bound_plan"]["accept"]
            and case["pred_sql"]
            and case["exec_equal"] is not True
        ),
        "bound_plan_nonroute_unexpected_sql": sum(
            1
            for case in cases
            if case["disposition"] != "route"
            and case["policies"]["bound_plan"]["accept"]
            and case["pred_sql"]
        ),
    }
    signals.update({f"stage_{stage}": count for stage, count in sorted(stage_counts.items())})

    return {
        "cases_total": len(cases),
        "route_total": route_total,
        "nonroute_total": nonroute_total,
        "policies": policies,
        "signals": signals,
        "current_wrong_by_family": _family_counts(
            cases, policy="current_permissive", bucket="wrong_sql", route=True
        ),
        "current_unexpected_by_family": _family_counts(
            cases, policy="current_permissive", bucket="unexpected_sql", route=False
        ),
        "frame_only_false_negative_by_family": _family_counts(
            cases, policy="frame_only", bucket="fail_closed", route=True
        ),
    }


def _variant_coverage(
    cases: list[dict[str, Any]], *, variants_per_route: int
) -> dict[str, Any]:
    base_route_cases = [
        case
        for case in cases
        if case["disposition"] == "route" and case.get("variant_kind") == "base"
    ]
    variant_route_cases = [
        case
        for case in cases
        if case["disposition"] == "route" and case.get("variant_kind") != "base"
    ]
    by_base: Counter[tuple[str, str]] = Counter()
    for case in variant_route_cases:
        variant_of = case.get("variant_of")
        if isinstance(variant_of, str) and variant_of:
            by_base[(str(case["suite"]), variant_of)] += 1

    missing: list[dict[str, Any]] = []
    at_requested = 0
    for case in base_route_cases:
        key = (str(case["suite"]), str(case["id"]))
        generated = by_base.get(key, 0)
        if generated >= variants_per_route:
            at_requested += 1
            continue
        missing.append(
            {
                "suite": case["suite"],
                "id": case["id"],
                "family": case["family"],
                "requested": variants_per_route,
                "generated": generated,
            }
        )

    return {
        "requested_per_route": variants_per_route,
        "base_route_cases": len(base_route_cases),
        "requested_route_variants": len(base_route_cases) * variants_per_route,
        "generated_route_variants": len(variant_route_cases),
        "route_cases_at_requested_variant_count": at_requested,
        "route_cases_below_requested_variant_count": len(missing),
        "missing_by_case": missing,
    }


def _count_policy(
    cases: list[dict[str, Any]], policy: str, disposition_group: str, bucket: str
) -> int:
    return sum(
        1
        for case in cases
        if (case["disposition"] == "route") == (disposition_group == "route")
        and case["policies"][policy]["bucket"] == bucket
    )


def _family_counts(
    cases: list[dict[str, Any]], *, policy: str, bucket: str, route: bool
) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for case in cases:
        if (case["disposition"] == "route") != route:
            continue
        if case["policies"][policy]["bucket"] == bucket:
            counts[str(case["family"])] += 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _append_variant_coverage(lines: list[str], coverage: object) -> None:
    if not isinstance(coverage, dict):
        return
    requested = int(coverage.get("requested_per_route") or 0)
    if requested <= 0:
        return

    lines.extend(
        [
            "",
            "## Variant Coverage",
            "",
            "| signal | count |",
            "|---|---:|",
            f"| `requested_per_route` | `{requested}` |",
            f"| `base_route_cases` | `{coverage.get('base_route_cases', 0)}` |",
            (
                "| `requested_route_variants` | "
                f"`{coverage.get('requested_route_variants', 0)}` |"
            ),
            (
                "| `generated_route_variants` | "
                f"`{coverage.get('generated_route_variants', 0)}` |"
            ),
            (
                "| `route_cases_at_requested_variant_count` | "
                f"`{coverage.get('route_cases_at_requested_variant_count', 0)}` |"
            ),
            (
                "| `route_cases_below_requested_variant_count` | "
                f"`{coverage.get('route_cases_below_requested_variant_count', 0)}` |"
            ),
            "",
        ]
    )

    missing = coverage.get("missing_by_case")
    if not isinstance(missing, list) or not missing:
        lines.extend(["All route cases met the requested variant count.", ""])
        return

    lines.extend(
        [
            "Route cases needing more generated paraphrase coverage:",
            "",
            "| suite | id | family | generated/requested |",
            "|---|---|---|---:|",
        ]
    )
    for row in missing:
        if not isinstance(row, dict):
            continue
        lines.append(
            "| `{suite}` | `{case_id}` | `{family}` | `{generated}/{requested}` |".format(
                suite=row.get("suite", ""),
                case_id=row.get("id", ""),
                family=row.get("family", ""),
                generated=row.get("generated", 0),
                requested=row.get("requested", requested),
            )
        )
    lines.append("")


def _append_backlog(lines: list[str], counts: dict[str, int], title: str) -> None:
    lines.extend([f"### {title}", ""])
    if not counts:
        lines.extend(["- none", ""])
        return
    for family, count in counts.items():
        lines.append(f"- `{family}`: `{count}`")
    lines.append("")
