"""Seeded QueryFrame production canary runner."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable, Sequence
from importlib import import_module
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from .cascade_runner import (
    CascadeQueryResult,
    CascadeRunnerError,
    build_graph_for_db,
    build_graph_for_db_url,
    make_cascade_predictor,
    run_cascade_query,
)
from .exec_acc import ExecResult, exec_results_eq, execute
from .fixtures import (
    build_queryframe_canary,
    write_queryframe_canary_mysql_sql,
    write_queryframe_canary_postgres_sql,
)
from .spider import Example, SpiderSuite

__all__ = [
    "render_queryframe_canary_markdown",
    "render_queryframe_canary_suite_markdown",
    "render_queryframe_mysql_canary_markdown",
    "render_queryframe_postgres_canary_markdown",
    "run_queryframe_canary",
    "run_queryframe_canary_suite",
    "run_queryframe_mysql_canary",
    "run_queryframe_postgres_canary",
]

_SENTINEL_SQL = "SELECT 1"


def run_queryframe_canary(
    *,
    out_dir: Path,
    seed: int,
    semsql_bin: Path,
    variant: str = "commerce",
    graph_cache_dir: Path | None = None,
    cascade_manifest: Path | None = None,
    intent_yaml: Path | None = None,
    query_timeout_seconds: int = 30,
    extract_timeout_seconds: int = 60,
    exec_timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Build and execute the seeded QueryFrame canary.

    The command is diagnostic by default: it reports routed accuracy and reject
    fail-closed behavior, while the CLI decides whether to enforce strict
    pass/fail semantics.
    """
    corpus_dir = build_queryframe_canary(out_dir, seed=seed, variant=variant)
    metadata_path = corpus_dir / "queryframe_canary.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    graph_root = graph_cache_dir or (corpus_dir / "graphs")
    frame_root = corpus_dir / "frames"
    if graph_cache_dir is None and graph_root.exists():
        shutil.rmtree(graph_root)
    if frame_root.exists():
        shutil.rmtree(frame_root)
    suite = SpiderSuite.load(corpus_dir / "dev.json", corpus_dir / "database")

    query_results: dict[tuple[str, str], CascadeQueryResult] = {}

    def on_query_result(example: Example, result: CascadeQueryResult) -> None:
        query_results[(example.db_id, example.question)] = result

    predict = make_cascade_predictor(
        semsql_bin=semsql_bin,
        graph_cache_dir=graph_root,
        extract_timeout_seconds=extract_timeout_seconds,
        query_timeout_seconds=query_timeout_seconds,
        on_query_result=on_query_result,
        cascade_manifest=cascade_manifest,
        intent_yaml=intent_yaml,
        query_frame_dir=frame_root,
    )

    routed_results = [
        _run_routed_case(
            index=index,
            example=example,
            predict=predict,
            query_result=query_results,
            exec_timeout_seconds=exec_timeout_seconds,
        )
        for index, example in enumerate(suite.examples)
    ]
    reject_results = _run_reject_cases(
        metadata=metadata,
        corpus_dir=corpus_dir,
        graph_root=graph_root,
        frame_root=frame_root,
        semsql_bin=semsql_bin,
        cascade_manifest=cascade_manifest,
        intent_yaml=intent_yaml,
        query_timeout_seconds=query_timeout_seconds,
        extract_timeout_seconds=extract_timeout_seconds,
    )
    routed_correct = sum(1 for row in routed_results if row["exec_equal"] is True)
    routed_total = len(routed_results)
    reject_closed = sum(1 for row in reject_results if row["fail_closed"] is True)
    reject_total = len(reject_results)
    return {
        "schema_version": 1,
        "seed": seed,
        "variant": metadata.get("variant", variant),
        "corpus_dir": str(corpus_dir),
        "metadata_path": str(metadata_path),
        "summary": {
            "routed_total": routed_total,
            "routed_correct": routed_correct,
            "routed_exec_acc": routed_correct / routed_total if routed_total else 0.0,
            "reject_total": reject_total,
            "reject_fail_closed": reject_closed,
            "reject_fail_closed_rate": (
                reject_closed / reject_total if reject_total else 0.0
            ),
            "pass": routed_correct == routed_total and reject_closed == reject_total,
        },
        "routed_cases": routed_results,
        "reject_cases": reject_results,
    }


def run_queryframe_canary_suite(
    *,
    out_dir: Path,
    seeds: Sequence[int],
    variants: Sequence[str],
    semsql_bin: Path,
    graph_cache_dir: Path | None = None,
    cascade_manifest: Path | None = None,
    intent_yaml: Path | None = None,
    query_timeout_seconds: int = 30,
    extract_timeout_seconds: int = 60,
    exec_timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Run a matrix of QueryFrame canaries and aggregate the stoplight."""
    runs: list[dict[str, Any]] = []
    for variant in variants:
        for seed in seeds:
            run_out = out_dir / f"{variant}-{seed}"
            run_graph_cache = (
                graph_cache_dir / f"{variant}-{seed}"
                if graph_cache_dir is not None
                else None
            )
            report = run_queryframe_canary(
                out_dir=run_out,
                seed=seed,
                semsql_bin=semsql_bin,
                variant=variant,
                graph_cache_dir=run_graph_cache,
                cascade_manifest=cascade_manifest,
                intent_yaml=intent_yaml,
                query_timeout_seconds=query_timeout_seconds,
                extract_timeout_seconds=extract_timeout_seconds,
                exec_timeout_seconds=exec_timeout_seconds,
            )
            runs.append(report)

    routed_total = sum(int(run["summary"]["routed_total"]) for run in runs)
    routed_correct = sum(int(run["summary"]["routed_correct"]) for run in runs)
    reject_total = sum(int(run["summary"]["reject_total"]) for run in runs)
    reject_closed = sum(int(run["summary"]["reject_fail_closed"]) for run in runs)
    passed_runs = sum(1 for run in runs if run["summary"]["pass"])
    return {
        "schema_version": 1,
        "seeds": list(seeds),
        "variants": list(variants),
        "summary": {
            "run_total": len(runs),
            "run_passed": passed_runs,
            "routed_total": routed_total,
            "routed_correct": routed_correct,
            "routed_exec_acc": routed_correct / routed_total if routed_total else 0.0,
            "reject_total": reject_total,
            "reject_fail_closed": reject_closed,
            "reject_fail_closed_rate": (
                reject_closed / reject_total if reject_total else 0.0
            ),
            "pass": passed_runs == len(runs),
        },
        "runs": runs,
    }


def run_queryframe_postgres_canary(
    *,
    out_dir: Path,
    seed: int,
    semsql_bin: Path,
    db_url: str | None = None,
    variant: str = "commerce",
    graph_cache_dir: Path | None = None,
    cascade_manifest: Path | None = None,
    intent_yaml: Path | None = None,
    query_timeout_seconds: int = 30,
    extract_timeout_seconds: int = 60,
    exec_timeout_seconds: float = 10.0,
    keep_schema: bool = False,
) -> dict[str, Any]:
    """Run the QueryFrame canary against live Postgres when configured.

    The command is deliberately opt-in: without a URL or a Python Postgres
    driver it returns a structured ``skipped`` report instead of pretending
    Postgres parity has been exercised.
    """
    corpus_dir = build_queryframe_canary(out_dir, seed=seed, variant=variant)
    metadata_path = corpus_dir / "queryframe_canary.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    schema = _postgres_canary_schema(metadata.get("variant", variant), seed)
    sql_paths = write_queryframe_canary_postgres_sql(
        corpus_dir,
        seed=seed,
        variant=variant,
        schema=schema,
    )
    resolved_url = db_url or os.environ.get("SEMSQL_POSTGRES_CANARY_URL")
    if not resolved_url:
        return _postgres_canary_skipped_report(
            corpus_dir=corpus_dir,
            metadata_path=metadata_path,
            sql_paths=sql_paths,
            seed=seed,
            variant=str(metadata.get("variant", variant)),
            schema=schema,
            reason="missing_db_url",
            detail="pass --db-url or set SEMSQL_POSTGRES_CANARY_URL",
        )
    try:
        psycopg = import_module("psycopg")
    except ImportError:
        return _postgres_canary_skipped_report(
            corpus_dir=corpus_dir,
            metadata_path=metadata_path,
            sql_paths=sql_paths,
            seed=seed,
            variant=str(metadata.get("variant", variant)),
            schema=schema,
            reason="missing_psycopg",
            detail="install psycopg in the eval environment",
        )

    graph_root = graph_cache_dir or (corpus_dir / "postgres-graphs")
    frame_root = corpus_dir / "postgres-frames"
    if graph_cache_dir is None and graph_root.exists():
        shutil.rmtree(graph_root)
    if frame_root.exists():
        shutil.rmtree(frame_root)
    graph_root.mkdir(parents=True, exist_ok=True)

    setup_sql = sql_paths["setup"].read_text(encoding="utf-8")
    teardown_sql = sql_paths["teardown"].read_text(encoding="utf-8")
    graph_url = _postgres_url_with_search_path(resolved_url, schema)
    suite = SpiderSuite.load(corpus_dir / "dev.json", corpus_dir / "database")
    graph_path = graph_root / f"{schema}.semsql"
    query_results: dict[tuple[str, str], CascadeQueryResult] = {}

    try:
        with psycopg.connect(resolved_url, autocommit=True) as conn:
            _postgres_execute_script(conn, setup_sql)
            build_graph_for_db_url(
                semsql_bin,
                graph_url,
                graph_path,
                path_arg=corpus_dir,
                timeout_seconds=extract_timeout_seconds,
            )
            routed_results = [
                _run_postgres_routed_case(
                    index=index,
                    example=example,
                    conn=conn,
                    schema=schema,
                    graph_path=graph_path,
                    semsql_bin=semsql_bin,
                    cascade_manifest=cascade_manifest,
                    intent_yaml=intent_yaml,
                    query_timeout_seconds=query_timeout_seconds,
                    exec_timeout_seconds=exec_timeout_seconds,
                    frame_root=frame_root,
                    query_results=query_results,
                )
                for index, example in enumerate(suite.examples)
            ]
            reject_results = _run_postgres_reject_cases(
                metadata=metadata,
                graph_path=graph_path,
                semsql_bin=semsql_bin,
                cascade_manifest=cascade_manifest,
                intent_yaml=intent_yaml,
                query_timeout_seconds=query_timeout_seconds,
                frame_root=frame_root,
            )
            if not keep_schema:
                _postgres_execute_script(conn, teardown_sql)
    except Exception as error:
        return _postgres_canary_error_report(
            corpus_dir=corpus_dir,
            metadata_path=metadata_path,
            sql_paths=sql_paths,
            seed=seed,
            variant=str(metadata.get("variant", variant)),
            schema=schema,
            error=error,
        )
    routed_correct = sum(1 for row in routed_results if row["exec_equal"] is True)
    routed_total = len(routed_results)
    reject_closed = sum(1 for row in reject_results if row["fail_closed"] is True)
    reject_total = len(reject_results)
    return {
        "schema_version": 1,
        "engine": "postgres",
        "status": "pass" if routed_correct == routed_total and reject_closed == reject_total else "fail",
        "seed": seed,
        "variant": metadata.get("variant", variant),
        "schema": schema,
        "corpus_dir": str(corpus_dir),
        "metadata_path": str(metadata_path),
        "setup_sql": str(sql_paths["setup"]),
        "teardown_sql": str(sql_paths["teardown"]),
        "summary": {
            "routed_total": routed_total,
            "routed_correct": routed_correct,
            "routed_exec_acc": routed_correct / routed_total if routed_total else 0.0,
            "reject_total": reject_total,
            "reject_fail_closed": reject_closed,
            "reject_fail_closed_rate": (
                reject_closed / reject_total if reject_total else 0.0
            ),
            "pass": routed_correct == routed_total and reject_closed == reject_total,
            "skipped": False,
        },
        "routed_cases": routed_results,
        "reject_cases": reject_results,
    }


def run_queryframe_mysql_canary(
    *,
    out_dir: Path,
    seed: int,
    semsql_bin: Path,
    db_url: str | None = None,
    variant: str = "commerce",
    graph_cache_dir: Path | None = None,
    cascade_manifest: Path | None = None,
    intent_yaml: Path | None = None,
    query_timeout_seconds: int = 30,
    extract_timeout_seconds: int = 60,
    exec_timeout_seconds: float = 10.0,
    keep_database: bool = False,
) -> dict[str, Any]:
    """Run the QueryFrame canary against live MySQL/MariaDB when configured."""
    corpus_dir = build_queryframe_canary(out_dir, seed=seed, variant=variant)
    metadata_path = corpus_dir / "queryframe_canary.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    database = _mysql_canary_database(metadata.get("variant", variant), seed)
    sql_paths = write_queryframe_canary_mysql_sql(
        corpus_dir,
        seed=seed,
        variant=variant,
        database=database,
    )
    resolved_url = db_url or os.environ.get("SEMSQL_MYSQL_CANARY_URL")
    if not resolved_url:
        return _sql_canary_skipped_report(
            engine="mysql",
            corpus_dir=corpus_dir,
            metadata_path=metadata_path,
            sql_paths=sql_paths,
            seed=seed,
            variant=str(metadata.get("variant", variant)),
            schema_or_database=database,
            reason="missing_db_url",
            detail="pass --db-url or set SEMSQL_MYSQL_CANARY_URL",
        )
    try:
        pymysql = import_module("pymysql")
    except ImportError:
        return _sql_canary_skipped_report(
            engine="mysql",
            corpus_dir=corpus_dir,
            metadata_path=metadata_path,
            sql_paths=sql_paths,
            seed=seed,
            variant=str(metadata.get("variant", variant)),
            schema_or_database=database,
            reason="missing_pymysql",
            detail="run with `uv run --extra db ...` or install pymysql",
        )

    graph_root = graph_cache_dir or (corpus_dir / "mysql-graphs")
    frame_root = corpus_dir / "mysql-frames"
    if graph_cache_dir is None and graph_root.exists():
        shutil.rmtree(graph_root)
    if frame_root.exists():
        shutil.rmtree(frame_root)
    graph_root.mkdir(parents=True, exist_ok=True)

    setup_sql = sql_paths["setup"].read_text(encoding="utf-8")
    teardown_sql = sql_paths["teardown"].read_text(encoding="utf-8")
    suite = SpiderSuite.load(corpus_dir / "dev.json", corpus_dir / "database")
    graph_path = graph_root / f"{database}.semsql"
    canary_url = _mysql_url_with_database(resolved_url, database)
    query_results: dict[tuple[str, str], CascadeQueryResult] = {}

    try:
        conn = _mysql_connect(pymysql, resolved_url, database=None, autocommit=True)
        try:
            _mysql_execute_script(conn, setup_sql)
            build_graph_for_db_url(
                semsql_bin,
                canary_url,
                graph_path,
                path_arg=corpus_dir,
                timeout_seconds=extract_timeout_seconds,
            )
            routed_results = [
                _run_mysql_routed_case(
                    index=index,
                    example=example,
                    conn=conn,
                    database=database,
                    graph_path=graph_path,
                    semsql_bin=semsql_bin,
                    cascade_manifest=cascade_manifest,
                    intent_yaml=intent_yaml,
                    query_timeout_seconds=query_timeout_seconds,
                    exec_timeout_seconds=exec_timeout_seconds,
                    frame_root=frame_root,
                    query_results=query_results,
                )
                for index, example in enumerate(suite.examples)
            ]
            reject_results = _run_mysql_reject_cases(
                metadata=metadata,
                graph_path=graph_path,
                semsql_bin=semsql_bin,
                cascade_manifest=cascade_manifest,
                intent_yaml=intent_yaml,
                query_timeout_seconds=query_timeout_seconds,
                frame_root=frame_root,
            )
            if not keep_database:
                _mysql_execute_script(conn, teardown_sql)
        finally:
            conn.close()
    except Exception as error:
        return _sql_canary_error_report(
            engine="mysql",
            corpus_dir=corpus_dir,
            metadata_path=metadata_path,
            sql_paths=sql_paths,
            seed=seed,
            variant=str(metadata.get("variant", variant)),
            schema_or_database=database,
            error=error,
        )
    routed_correct = sum(1 for row in routed_results if row["exec_equal"] is True)
    routed_total = len(routed_results)
    reject_closed = sum(1 for row in reject_results if row["fail_closed"] is True)
    reject_total = len(reject_results)
    return {
        "schema_version": 1,
        "engine": "mysql",
        "status": "pass" if routed_correct == routed_total and reject_closed == reject_total else "fail",
        "seed": seed,
        "variant": metadata.get("variant", variant),
        "database": database,
        "corpus_dir": str(corpus_dir),
        "metadata_path": str(metadata_path),
        "setup_sql": str(sql_paths["setup"]),
        "teardown_sql": str(sql_paths["teardown"]),
        "summary": {
            "routed_total": routed_total,
            "routed_correct": routed_correct,
            "routed_exec_acc": routed_correct / routed_total if routed_total else 0.0,
            "reject_total": reject_total,
            "reject_fail_closed": reject_closed,
            "reject_fail_closed_rate": (
                reject_closed / reject_total if reject_total else 0.0
            ),
            "pass": routed_correct == routed_total and reject_closed == reject_total,
            "skipped": False,
        },
        "routed_cases": routed_results,
        "reject_cases": reject_results,
    }


def _run_routed_case(
    *,
    index: int,
    example: Example,
    predict: Callable[[Example], str],
    query_result: dict[tuple[str, str], CascadeQueryResult],
    exec_timeout_seconds: float,
) -> dict[str, Any]:
    pred_sql = predict(example)
    result = query_result.get((example.db_id, example.question))
    stage = result.stage_pinned if result is not None else "unknown"
    if _is_sentinel(pred_sql):
        return {
            "index": index,
            "db_id": example.db_id,
            "question": example.question,
            "gold_sql": example.gold_sql,
            "pred_sql": pred_sql,
            "exec_equal": False,
            "bucket": "bailed",
            "stage_pinned": stage,
            "error_detail": result.error_detail if result is not None else None,
        }
    gold = execute(example.db_path, example.gold_sql, timeout_seconds=exec_timeout_seconds)
    pred = execute(example.db_path, pred_sql, timeout_seconds=exec_timeout_seconds)
    exec_equal = exec_results_eq(example.gold_sql, gold, pred)
    if gold.is_error:
        bucket = "gold_exec_error"
    elif pred.is_error:
        bucket = "pred_exec_error"
    elif exec_equal:
        bucket = "correct"
    else:
        bucket = "exec_mismatch"
    return {
        "index": index,
        "db_id": example.db_id,
        "question": example.question,
        "gold_sql": example.gold_sql,
        "pred_sql": pred_sql,
        "exec_equal": exec_equal,
        "bucket": bucket,
        "stage_pinned": stage,
        "gold_error": gold.error,
        "pred_error": pred.error,
    }


def _run_reject_cases(
    *,
    metadata: dict[str, Any],
    corpus_dir: Path,
    graph_root: Path,
    frame_root: Path,
    semsql_bin: Path,
    cascade_manifest: Path | None,
    intent_yaml: Path | None,
    query_timeout_seconds: int,
    extract_timeout_seconds: int,
) -> list[dict[str, Any]]:
    reject_cases = metadata.get("reject_cases", [])
    if not isinstance(reject_cases, list):
        return []
    graph_paths: dict[str, Path] = {}
    results: list[dict[str, Any]] = []
    for index, case in enumerate(reject_cases):
        if not isinstance(case, dict):
            continue
        db_id = str(case.get("db_id", ""))
        question = str(case.get("question", ""))
        if not db_id or not question:
            continue
        db_path = corpus_dir / "database" / db_id / f"{db_id}.sqlite"
        graph_path = graph_paths.get(db_id)
        if graph_path is None:
            graph_path = graph_root / f"{db_id}.semsql"
            try:
                build_graph_for_db(
                    semsql_bin,
                    db_path,
                    graph_path,
                    timeout_seconds=extract_timeout_seconds,
                )
            except CascadeRunnerError as error:
                results.append(
                    {
                        "index": index,
                        "db_id": db_id,
                        "question": question,
                        "kind": case.get("kind"),
                        "fail_closed": True,
                        "bucket": "graph_extract_failed",
                        "error_detail": str(error),
                    }
                )
                continue
            graph_paths[db_id] = graph_path
        frame_path = frame_root / "rejects" / f"{index:03d}.json"
        result = run_cascade_query(
            semsql_bin,
            graph_path,
            question,
            timeout_seconds=query_timeout_seconds,
            cascade_manifest=cascade_manifest,
            intent_yaml=intent_yaml,
            query_frame_json=frame_path,
        )
        fail_closed = result.sql is None
        results.append(
            {
                "index": index,
                "db_id": db_id,
                "question": question,
                "kind": case.get("kind"),
                "reason": case.get("reason"),
                "fail_closed": fail_closed,
                "bucket": "rejected" if fail_closed else "unexpected_route",
                "stage_pinned": result.stage_pinned,
                "pred_sql": result.sql,
                "error_detail": result.error_detail,
            }
        )
    return results


def _run_postgres_routed_case(
    *,
    index: int,
    example: Example,
    conn: Any,
    schema: str,
    graph_path: Path,
    semsql_bin: Path,
    cascade_manifest: Path | None,
    intent_yaml: Path | None,
    query_timeout_seconds: int,
    exec_timeout_seconds: float,
    frame_root: Path,
    query_results: dict[tuple[str, str], CascadeQueryResult],
) -> dict[str, Any]:
    frame_path = frame_root / "routed" / f"{index:03d}.json"
    result = run_cascade_query(
        semsql_bin,
        graph_path,
        example.question,
        timeout_seconds=query_timeout_seconds,
        cascade_manifest=cascade_manifest,
        intent_yaml=intent_yaml,
        dialect="postgres",
        query_frame_json=frame_path,
    )
    query_results[(example.db_id, example.question)] = result
    stage = result.stage_pinned
    if result.sql is None or _is_sentinel(result.sql):
        return {
            "index": index,
            "db_id": example.db_id,
            "question": example.question,
            "gold_sql": example.gold_sql,
            "pred_sql": result.sql or _SENTINEL_SQL,
            "exec_equal": False,
            "bucket": "bailed",
            "stage_pinned": stage,
            "error_detail": result.error_detail,
        }
    gold = _postgres_execute(
        conn,
        schema,
        example.gold_sql,
        timeout_seconds=exec_timeout_seconds,
    )
    pred = _postgres_execute(
        conn,
        schema,
        result.sql,
        timeout_seconds=exec_timeout_seconds,
    )
    exec_equal = exec_results_eq(example.gold_sql, gold, pred)
    if gold.is_error:
        bucket = "gold_exec_error"
    elif pred.is_error:
        bucket = "pred_exec_error"
    elif exec_equal:
        bucket = "correct"
    else:
        bucket = "exec_mismatch"
    return {
        "index": index,
        "db_id": example.db_id,
        "question": example.question,
        "gold_sql": example.gold_sql,
        "pred_sql": result.sql,
        "exec_equal": exec_equal,
        "bucket": bucket,
        "stage_pinned": stage,
        "gold_error": gold.error,
        "pred_error": pred.error,
    }


def _run_postgres_reject_cases(
    *,
    metadata: dict[str, Any],
    graph_path: Path,
    semsql_bin: Path,
    cascade_manifest: Path | None,
    intent_yaml: Path | None,
    query_timeout_seconds: int,
    frame_root: Path,
) -> list[dict[str, Any]]:
    reject_cases = metadata.get("reject_cases", [])
    if not isinstance(reject_cases, list):
        return []
    results: list[dict[str, Any]] = []
    for index, case in enumerate(reject_cases):
        if not isinstance(case, dict):
            continue
        question = str(case.get("question", ""))
        if not question:
            continue
        frame_path = frame_root / "rejects" / f"{index:03d}.json"
        result = run_cascade_query(
            semsql_bin,
            graph_path,
            question,
            timeout_seconds=query_timeout_seconds,
            cascade_manifest=cascade_manifest,
            intent_yaml=intent_yaml,
            dialect="postgres",
            query_frame_json=frame_path,
        )
        fail_closed = result.sql is None
        results.append(
            {
                "index": index,
                "db_id": case.get("db_id"),
                "question": question,
                "kind": case.get("kind"),
                "reason": case.get("reason"),
                "fail_closed": fail_closed,
                "bucket": "rejected" if fail_closed else "unexpected_route",
                "stage_pinned": result.stage_pinned,
                "pred_sql": result.sql,
                "error_detail": result.error_detail,
            }
        )
    return results


def _run_mysql_routed_case(
    *,
    index: int,
    example: Example,
    conn: Any,
    database: str,
    graph_path: Path,
    semsql_bin: Path,
    cascade_manifest: Path | None,
    intent_yaml: Path | None,
    query_timeout_seconds: int,
    exec_timeout_seconds: float,
    frame_root: Path,
    query_results: dict[tuple[str, str], CascadeQueryResult],
) -> dict[str, Any]:
    frame_path = frame_root / "routed" / f"{index:03d}.json"
    result = run_cascade_query(
        semsql_bin,
        graph_path,
        example.question,
        timeout_seconds=query_timeout_seconds,
        cascade_manifest=cascade_manifest,
        intent_yaml=intent_yaml,
        dialect="mysql",
        query_frame_json=frame_path,
    )
    query_results[(example.db_id, example.question)] = result
    stage = result.stage_pinned
    if result.sql is None or _is_sentinel(result.sql):
        return {
            "index": index,
            "db_id": example.db_id,
            "question": example.question,
            "gold_sql": example.gold_sql,
            "pred_sql": result.sql or _SENTINEL_SQL,
            "exec_equal": False,
            "bucket": "bailed",
            "stage_pinned": stage,
            "error_detail": result.error_detail,
        }
    gold = _mysql_execute(
        conn,
        database,
        example.gold_sql,
        timeout_seconds=exec_timeout_seconds,
    )
    pred = _mysql_execute(
        conn,
        database,
        result.sql,
        timeout_seconds=exec_timeout_seconds,
    )
    exec_equal = exec_results_eq(example.gold_sql, gold, pred)
    if gold.is_error:
        bucket = "gold_exec_error"
    elif pred.is_error:
        bucket = "pred_exec_error"
    elif exec_equal:
        bucket = "correct"
    else:
        bucket = "exec_mismatch"
    return {
        "index": index,
        "db_id": example.db_id,
        "question": example.question,
        "gold_sql": example.gold_sql,
        "pred_sql": result.sql,
        "exec_equal": exec_equal,
        "bucket": bucket,
        "stage_pinned": stage,
        "gold_error": gold.error,
        "pred_error": pred.error,
    }


def _run_mysql_reject_cases(
    *,
    metadata: dict[str, Any],
    graph_path: Path,
    semsql_bin: Path,
    cascade_manifest: Path | None,
    intent_yaml: Path | None,
    query_timeout_seconds: int,
    frame_root: Path,
) -> list[dict[str, Any]]:
    reject_cases = metadata.get("reject_cases", [])
    if not isinstance(reject_cases, list):
        return []
    results: list[dict[str, Any]] = []
    for index, case in enumerate(reject_cases):
        if not isinstance(case, dict):
            continue
        question = str(case.get("question", ""))
        if not question:
            continue
        frame_path = frame_root / "rejects" / f"{index:03d}.json"
        result = run_cascade_query(
            semsql_bin,
            graph_path,
            question,
            timeout_seconds=query_timeout_seconds,
            cascade_manifest=cascade_manifest,
            intent_yaml=intent_yaml,
            dialect="mysql",
            query_frame_json=frame_path,
        )
        fail_closed = result.sql is None
        results.append(
            {
                "index": index,
                "db_id": case.get("db_id"),
                "question": question,
                "kind": case.get("kind"),
                "reason": case.get("reason"),
                "fail_closed": fail_closed,
                "bucket": "rejected" if fail_closed else "unexpected_route",
                "stage_pinned": result.stage_pinned,
                "pred_sql": result.sql,
                "error_detail": result.error_detail,
            }
        )
    return results


def render_queryframe_canary_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    routed_total = summary["routed_total"]
    routed_correct = summary["routed_correct"]
    reject_total = summary["reject_total"]
    reject_closed = summary["reject_fail_closed"]
    status = "PASS" if summary["pass"] else "FAIL"
    lines = [
        "# QueryFrame Canary",
        "",
        f"- status: `{status}`",
        f"- seed: `{report['seed']}`",
        f"- variant: `{report.get('variant', 'commerce')}`",
        f"- routed exec accuracy: `{routed_correct}/{routed_total}`",
        f"- reject fail-closed: `{reject_closed}/{reject_total}`",
        f"- corpus: `{report['corpus_dir']}`",
        "",
        "## Routed Buckets",
        "",
    ]
    routed_buckets = _count_buckets(report["routed_cases"], "bucket")
    for bucket, count in sorted(routed_buckets.items()):
        lines.append(f"- `{bucket}`: `{count}`")
    lines.extend(["", "## Reject Buckets", ""])
    reject_buckets = _count_buckets(report["reject_cases"], "bucket")
    for bucket, count in sorted(reject_buckets.items()):
        lines.append(f"- `{bucket}`: `{count}`")
    lines.append("")
    return "\n".join(lines)


def render_queryframe_postgres_canary_markdown(report: dict[str, Any]) -> str:
    status = str(report.get("status", "unknown")).upper()
    summary = report["summary"]
    lines = [
        "# QueryFrame Postgres Canary",
        "",
        f"- status: `{status}`",
        f"- seed: `{report['seed']}`",
        f"- variant: `{report.get('variant', 'commerce')}`",
        f"- schema: `{report.get('schema')}`",
        f"- corpus: `{report['corpus_dir']}`",
        f"- setup SQL: `{report.get('setup_sql')}`",
        "",
    ]
    if summary.get("skipped"):
        lines.extend(
            [
                "## Skip Reason",
                "",
                f"- reason: `{report.get('skip_reason')}`",
                f"- detail: {report.get('skip_detail')}",
                "",
            ]
        )
        return "\n".join(lines)
    if report.get("status") == "error":
        lines.extend(
            [
                "## Error",
                "",
                f"- detail: {report.get('error_detail')}",
                "",
            ]
        )
        return "\n".join(lines)
    lines.extend(
        [
            f"- routed exec accuracy: `{summary['routed_correct']}/{summary['routed_total']}`",
            f"- reject fail-closed: `{summary['reject_fail_closed']}/{summary['reject_total']}`",
            "",
            "## Routed Buckets",
            "",
        ]
    )
    routed_buckets = _count_buckets(report["routed_cases"], "bucket")
    for bucket, count in sorted(routed_buckets.items()):
        lines.append(f"- `{bucket}`: `{count}`")
    lines.extend(["", "## Reject Buckets", ""])
    reject_buckets = _count_buckets(report["reject_cases"], "bucket")
    for bucket, count in sorted(reject_buckets.items()):
        lines.append(f"- `{bucket}`: `{count}`")
    lines.append("")
    return "\n".join(lines)


def render_queryframe_mysql_canary_markdown(report: dict[str, Any]) -> str:
    status = str(report.get("status", "unknown")).upper()
    summary = report["summary"]
    lines = [
        "# QueryFrame MySQL/MariaDB Canary",
        "",
        f"- status: `{status}`",
        f"- seed: `{report['seed']}`",
        f"- variant: `{report.get('variant', 'commerce')}`",
        f"- database: `{report.get('database')}`",
        f"- corpus: `{report['corpus_dir']}`",
        f"- setup SQL: `{report.get('setup_sql')}`",
        "",
    ]
    if summary.get("skipped"):
        lines.extend(
            [
                "## Skip Reason",
                "",
                f"- reason: `{report.get('skip_reason')}`",
                f"- detail: {report.get('skip_detail')}",
                "",
            ]
        )
        return "\n".join(lines)
    if report.get("status") == "error":
        lines.extend(
            [
                "## Error",
                "",
                f"- detail: {report.get('error_detail')}",
                "",
            ]
        )
        return "\n".join(lines)
    lines.extend(
        [
            f"- routed exec accuracy: `{summary['routed_correct']}/{summary['routed_total']}`",
            f"- reject fail-closed: `{summary['reject_fail_closed']}/{summary['reject_total']}`",
            "",
            "## Routed Buckets",
            "",
        ]
    )
    routed_buckets = _count_buckets(report["routed_cases"], "bucket")
    for bucket, count in sorted(routed_buckets.items()):
        lines.append(f"- `{bucket}`: `{count}`")
    lines.extend(["", "## Reject Buckets", ""])
    reject_buckets = _count_buckets(report["reject_cases"], "bucket")
    for bucket, count in sorted(reject_buckets.items()):
        lines.append(f"- `{bucket}`: `{count}`")
    lines.append("")
    return "\n".join(lines)


def render_queryframe_canary_suite_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    status = "PASS" if summary["pass"] else "FAIL"
    lines = [
        "# QueryFrame Canary Suite",
        "",
        f"- status: `{status}`",
        f"- variants: `{', '.join(report['variants'])}`",
        f"- seeds: `{', '.join(str(seed) for seed in report['seeds'])}`",
        f"- runs: `{summary['run_passed']}/{summary['run_total']}`",
        (
            "- routed exec accuracy: "
            f"`{summary['routed_correct']}/{summary['routed_total']}`"
        ),
        (
            "- reject fail-closed: "
            f"`{summary['reject_fail_closed']}/{summary['reject_total']}`"
        ),
        "",
        "## Runs",
        "",
        "| variant | seed | routed | rejects | status |",
        "|---|---:|---:|---:|---|",
    ]
    for run in report["runs"]:
        run_summary = run["summary"]
        run_status = "PASS" if run_summary["pass"] else "FAIL"
        lines.append(
            "| "
            f"`{run.get('variant', 'commerce')}` | "
            f"`{run['seed']}` | "
            f"`{run_summary['routed_correct']}/{run_summary['routed_total']}` | "
            f"`{run_summary['reject_fail_closed']}/{run_summary['reject_total']}` | "
            f"`{run_status}` |"
        )
    failing = [run for run in report["runs"] if not run["summary"]["pass"]]
    if failing:
        lines.extend(["", "## Failures", ""])
        for run in failing:
            lines.append(f"### `{run.get('variant', 'commerce')}` seed `{run['seed']}`")
            for row in run["routed_cases"]:
                if row.get("exec_equal") is True:
                    continue
                lines.append(
                    f"- `{row.get('bucket')}`: {row.get('question')} "
                    f"(stage `{row.get('stage_pinned')}`)"
                )
            for row in run["reject_cases"]:
                if row.get("fail_closed") is True:
                    continue
                lines.append(
                    f"- `{row.get('bucket')}`: {row.get('question')} "
                    f"(stage `{row.get('stage_pinned')}`)"
                )
    lines.append("")
    return "\n".join(lines)


def _count_buckets(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        bucket = str(row.get(key, "unknown"))
        counts[bucket] = counts.get(bucket, 0) + 1
    return counts


def _is_sentinel(sql: str) -> bool:
    return " ".join(sql.split()).strip().rstrip(";").strip().lower() == "select 1"


def _postgres_execute(
    conn: Any,
    schema: str,
    sql: str,
    *,
    timeout_seconds: float,
) -> ExecResult:
    try:
        timeout_ms = max(1, int(timeout_seconds * 1000))
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = {timeout_ms}")
            cur.execute(f"SET search_path TO {_quote_pg_ident(schema)}, public")
            cur.execute(sql)
            rows = tuple(tuple(row) for row in cur.fetchall())
            column_count = len(cur.description or ())
        return ExecResult(rows=rows, column_count=column_count)
    except Exception as error:
        return ExecResult(rows=(), column_count=0, error=str(error))


def _postgres_execute_script(conn: Any, sql: str) -> None:
    statements = [part.strip() for part in sql.split(";") if part.strip()]
    with conn.cursor() as cur:
        for statement in statements:
            cur.execute(statement)


def _mysql_execute(
    conn: Any,
    database: str,
    sql: str,
    *,
    timeout_seconds: float,
) -> ExecResult:
    _ = timeout_seconds
    try:
        with conn.cursor() as cur:
            cur.execute(f"USE {_quote_mysql_ident(database)}")
            cur.execute(sql)
            rows = tuple(tuple(row) for row in cur.fetchall())
            column_count = len(cur.description or ())
        return ExecResult(rows=rows, column_count=column_count)
    except Exception as error:
        return ExecResult(rows=(), column_count=0, error=str(error))


def _mysql_execute_script(conn: Any, sql: str) -> None:
    statements = [part.strip() for part in sql.split(";") if part.strip()]
    with conn.cursor() as cur:
        for statement in statements:
            cur.execute(statement)


def _mysql_connect(
    pymysql: Any,
    db_url: str,
    *,
    database: str | None,
    autocommit: bool,
) -> Any:
    parts = urlsplit(db_url)
    if parts.scheme not in {"mysql", "mariadb"}:
        raise ValueError("MySQL canary db URL must use mysql:// or mariadb://")
    if not parts.hostname:
        raise ValueError("MySQL canary db URL must include a host")
    username = unquote(parts.username or "")
    if not username:
        raise ValueError("MySQL canary db URL must include a username")
    password = unquote(parts.password or "") if parts.password is not None else None
    return pymysql.connect(
        host=parts.hostname,
        port=parts.port or 3306,
        user=username,
        password=password,
        database=database,
        autocommit=autocommit,
        charset="utf8mb4",
    )


def _postgres_canary_schema(variant: str, seed: int) -> str:
    safe_variant = "".join(ch if ch.isalnum() else "_" for ch in variant.lower())
    return f"semsql_qf_{safe_variant}_{seed}"


def _mysql_canary_database(variant: str, seed: int) -> str:
    safe_variant = "".join(ch if ch.isalnum() else "_" for ch in variant.lower())
    return f"semsql_qf_{safe_variant}_{seed}"


def _postgres_url_with_search_path(db_url: str, schema: str) -> str:
    parts = urlsplit(db_url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query = [(key, value) for key, value in query if key.lower() != "options"]
    query.append(("options", f"-csearch_path={schema},public"))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _mysql_url_with_database(db_url: str, database: str) -> str:
    parts = urlsplit(db_url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{database}", parts.query, parts.fragment))


def _sql_canary_skipped_report(
    *,
    engine: str,
    corpus_dir: Path,
    metadata_path: Path,
    sql_paths: dict[str, Path],
    seed: int,
    variant: str,
    schema_or_database: str,
    reason: str,
    detail: str,
) -> dict[str, Any]:
    location_key = "database" if engine == "mysql" else "schema"
    return {
        "schema_version": 1,
        "engine": engine,
        "status": "skipped",
        "skip_reason": reason,
        "skip_detail": detail,
        "seed": seed,
        "variant": variant,
        location_key: schema_or_database,
        "corpus_dir": str(corpus_dir),
        "metadata_path": str(metadata_path),
        "setup_sql": str(sql_paths["setup"]),
        "teardown_sql": str(sql_paths["teardown"]),
        "summary": {
            "routed_total": 0,
            "routed_correct": 0,
            "routed_exec_acc": 0.0,
            "reject_total": 0,
            "reject_fail_closed": 0,
            "reject_fail_closed_rate": 0.0,
            "pass": False,
            "skipped": True,
        },
        "routed_cases": [],
        "reject_cases": [],
    }


def _sql_canary_error_report(
    *,
    engine: str,
    corpus_dir: Path,
    metadata_path: Path,
    sql_paths: dict[str, Path],
    seed: int,
    variant: str,
    schema_or_database: str,
    error: Exception,
) -> dict[str, Any]:
    location_key = "database" if engine == "mysql" else "schema"
    return {
        "schema_version": 1,
        "engine": engine,
        "status": "error",
        "error_detail": str(error),
        "seed": seed,
        "variant": variant,
        location_key: schema_or_database,
        "corpus_dir": str(corpus_dir),
        "metadata_path": str(metadata_path),
        "setup_sql": str(sql_paths["setup"]),
        "teardown_sql": str(sql_paths["teardown"]),
        "summary": {
            "routed_total": 0,
            "routed_correct": 0,
            "routed_exec_acc": 0.0,
            "reject_total": 0,
            "reject_fail_closed": 0,
            "reject_fail_closed_rate": 0.0,
            "pass": False,
            "skipped": False,
        },
        "routed_cases": [],
        "reject_cases": [],
    }


def _postgres_canary_skipped_report(
    *,
    corpus_dir: Path,
    metadata_path: Path,
    sql_paths: dict[str, Path],
    seed: int,
    variant: str,
    schema: str,
    reason: str,
    detail: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "engine": "postgres",
        "status": "skipped",
        "skip_reason": reason,
        "skip_detail": detail,
        "seed": seed,
        "variant": variant,
        "schema": schema,
        "corpus_dir": str(corpus_dir),
        "metadata_path": str(metadata_path),
        "setup_sql": str(sql_paths["setup"]),
        "teardown_sql": str(sql_paths["teardown"]),
        "summary": {
            "routed_total": 0,
            "routed_correct": 0,
            "routed_exec_acc": 0.0,
            "reject_total": 0,
            "reject_fail_closed": 0,
            "reject_fail_closed_rate": 0.0,
            "pass": False,
            "skipped": True,
        },
        "routed_cases": [],
        "reject_cases": [],
    }


def _postgres_canary_error_report(
    *,
    corpus_dir: Path,
    metadata_path: Path,
    sql_paths: dict[str, Path],
    seed: int,
    variant: str,
    schema: str,
    error: Exception,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "engine": "postgres",
        "status": "error",
        "error_detail": str(error),
        "seed": seed,
        "variant": variant,
        "schema": schema,
        "corpus_dir": str(corpus_dir),
        "metadata_path": str(metadata_path),
        "setup_sql": str(sql_paths["setup"]),
        "teardown_sql": str(sql_paths["teardown"]),
        "summary": {
            "routed_total": 0,
            "routed_correct": 0,
            "routed_exec_acc": 0.0,
            "reject_total": 0,
            "reject_fail_closed": 0,
            "reject_fail_closed_rate": 0.0,
            "pass": False,
            "skipped": False,
        },
        "routed_cases": [],
        "reject_cases": [],
    }


def _quote_pg_ident(name: str) -> str:
    if not name:
        raise ValueError("Postgres identifier cannot be empty")
    if "\x00" in name:
        raise ValueError(f"unsafe Postgres identifier {name!r}")
    return '"' + name.replace('"', '""') + '"'


def _quote_mysql_ident(name: str) -> str:
    if not name:
        raise ValueError("MySQL identifier cannot be empty")
    if "\x00" in name:
        raise ValueError(f"unsafe MySQL identifier {name!r}")
    return "`" + name.replace("`", "``") + "`"
