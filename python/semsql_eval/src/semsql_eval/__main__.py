"""Command-line entry for the eval harness.

Subcommands:

    python -m semsql_eval spider --questions ... --db-root ...
        Run Spider/BIRD-style evaluation against the cascade. Builds a
        per-DB SemanticGraph on first use and caches it in
        --graph-cache-dir.

    python -m semsql_eval bypass-corpus
        Print a summary of the bypass-corpus integration status — how
        many cases the rewriter scopes, how many the Rust second-pass
        accepts. Useful for `semsql doctor` parity checks.

The ML extras (torch, transformers) are not required for report/eval
orchestration. Spider/BIRD eval shells out to the `semsql` binary, so ONNX
runtime behavior comes from the compiled CLI and selected cascade manifest.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from importlib import import_module
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote, urlsplit

import click

from . import __version__ as SEMSQL_EVAL_VERSION
from .ablation_gap import (
    ablation_gap_report,
    ablation_gap_report_to_json,
    render_ablation_gap_markdown,
)
from .binder_probe import (
    render_binder_probe_markdown,
    run_binder_probe,
)
from .cascade_runner import build_graph_for_db_url, make_cascade_predictor, run_cascade_query
from .datasets import (
    BIRD_TRAIN_URL,
    DEFAULT_BIRD_TRAIN_MIN_FREE_GB,
    bird_train_is_materialized,
    download_file,
    ensure_min_free_space,
    gibibytes,
    materialize_official_bird_train_archive,
    safe_extract_zip,
)
from .exec_acc import exec_results_eq, execute
from .fixtures import build_corpus, build_queryframe_canary
from .framework_bridge_probe import (
    render_framework_bridge_probe_markdown,
    run_framework_bridge_probe,
)
from .llm_resolution import (
    DEFAULT_DEEPSEEK_BASE_URL,
    DEFAULT_DEEPSEEK_MODEL,
    DEFAULT_GROQ_BASE_URL,
    DEFAULT_GROQ_MODEL,
    DEFAULT_OPENAI_MODEL,
    build_openai_resolution_request,
    build_openai_resolution_request_batch,
    build_pathway_rejected_query_packets,
    build_rejected_query_packet,
    build_runtime_frame_resolution_proposal,
    build_schema_card,
    call_openai_chat_compatible_resolution,
    call_openai_resolution,
    evaluate_resolution_safety_expectations,
    render_openai_request_batch_markdown,
    render_pathway_packet_index_markdown,
    render_resolution_batch_markdown,
    render_resolution_proposal,
    render_resolution_proposal_batch,
    render_resolution_provider_batch_markdown,
    render_resolution_safety_expectations_markdown,
    render_schema_card_markdown,
    resolve_resolution_proposal_batch,
    validate_resolution_proposal,
)
from .oracle_gap import (
    oracle_gap_report,
    oracle_gap_report_to_json,
    render_oracle_gap_markdown,
)
from .package_bridge_probe import (
    render_package_bridge_probe_markdown,
    run_package_bridge_probe,
)
from .package_dlx_smoke import (
    render_package_dlx_smoke_markdown,
    run_package_dlx_smoke,
)
from .package_launcher_smoke import (
    render_package_launcher_smoke_markdown,
    run_package_launcher_smoke,
)
from .package_public_smoke import (
    render_package_public_smoke_markdown,
    run_package_public_smoke,
)
from .package_registry_smoke import (
    render_package_registry_smoke_markdown,
    run_package_registry_smoke,
)
from .pathway_benchmark import (
    pathway_product_gate_failures,
    render_pathway_benchmark_markdown,
    run_pathway_benchmark,
)
from .platform_suite import (
    build_business_analytics_suite,
    build_platform_query_suite,
    render_platform_suite_markdown,
)
from .production_readiness import (
    build_production_readiness_report,
    load_json_report,
    render_production_readiness_markdown,
)
from .queryframe_canary import (
    render_queryframe_canary_markdown,
    render_queryframe_canary_suite_markdown,
    render_queryframe_mysql_canary_markdown,
    render_queryframe_postgres_canary_markdown,
    run_queryframe_canary,
    run_queryframe_canary_suite,
    run_queryframe_mysql_canary,
    run_queryframe_postgres_canary,
)
from .queryframe_probe import (
    render_queryframe_probe_markdown,
    run_queryframe_probe,
)
from .real_app_framework_probe import (
    render_real_app_framework_probe_markdown,
    run_real_app_framework_probe,
)
from .realdb_schema_probe import (
    SAFE_IDENTIFIER_RE,
    _ambiguous_physical_family_tables,
    _database_from_url,
    _graph_sample_value_count,
    _graph_sample_values,
    _import_postgres_driver,
    _list_columns,
    _list_postgres_columns,
    _list_postgres_relationships,
    _list_postgres_tables,
    _list_relationships,
    _list_tables,
    _mysql_connect,
    _mysql_url_with_database,
    _postgres_connect,
    _postgres_current_database,
    _postgres_url_with_database,
    _select_random_database,
    html_escape,
    name_looks_sensitive,
    redact_db_url,
    render_mysql_realdb_schema_probe_markdown,
    render_mysql_realdb_schema_probe_suite_markdown,
    render_postgres_realdb_schema_probe_markdown,
    render_postgres_realdb_schema_probe_suite_markdown,
    run_mysql_realdb_schema_probe,
    run_mysql_realdb_schema_probe_suite,
    run_postgres_realdb_schema_probe,
    run_postgres_realdb_schema_probe_suite,
    select_typed_fallback_filtered_grouped_metric_questions,
    select_typed_fallback_grouped_metric_questions,
    select_typed_fallback_joined_filtered_grouped_metric_questions,
    select_typed_fallback_multi_joined_filtered_grouped_metric_questions,
    select_typed_fallback_multi_series_metric_questions,
    select_typed_fallback_rate_questions,
    select_typed_fallback_value_filtered_grouped_metric_questions,
)
from .report_diagnostics import (
    diagnose_report,
    diagnosis_report_to_json,
    render_diagnosis_markdown,
)
from .semantic_atlas_assessment import (
    render_semantic_atlas_assessment_markdown,
    run_semantic_atlas_assessment,
)
from .sharding_audit import (
    render_mysql_sharding_audit_markdown,
    run_mysql_sharding_audit,
)
from .spider import EvalSummary, Example, SpiderSuite

TYPED_PROVIDER_CHOICES = ["openai", "openai-compatible", "groq", "deepseek"]


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def cli() -> None:
    """SemanticSQL evaluation harness."""


@cli.command("framework-bridge-probe")
@click.option(
    "--out",
    "out_dir",
    type=click.Path(path_type=Path),
    required=True,
    help="Output directory for generated framework fixtures and graphs.",
)
@click.option(
    "--semsql-bin",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Native semsql binary to exercise.",
)
@click.option(
    "--out-json",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional JSON report path.",
)
@click.option(
    "--out-md",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional Markdown report path.",
)
@click.option(
    "--build-extractor/--no-build-extractor",
    default=False,
    show_default=True,
    help="Run `pnpm --filter @semsql/extractor-cli build` before probing.",
)
def framework_bridge_probe_cmd(
    out_dir: Path,
    semsql_bin: Path,
    out_json: Path | None,
    out_md: Path | None,
    build_extractor: bool,
) -> None:
    """Probe framework-source aliases through native `semsql extract`."""
    report = run_framework_bridge_probe(
        out_dir=out_dir,
        semsql_bin=semsql_bin,
        build_extractor=build_extractor,
    )
    rendered = render_framework_bridge_probe_markdown(report)
    click.echo(rendered.rstrip())
    if out_json is not None:
        _write_json_report(out_json, report)
    if out_md is not None:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(rendered, encoding="utf-8")
    if report["status"] != "pass":
        raise click.ClickException("framework bridge probe failed")


@cli.command("package-bridge-probe")
@click.option(
    "--out",
    "out_dir",
    type=click.Path(path_type=Path),
    required=True,
    help="Output directory for generated package-bridge fixtures and graphs.",
)
@click.option(
    "--semsql-bin",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Native semsql binary to exercise.",
)
@click.option(
    "--out-json",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional JSON report path.",
)
@click.option(
    "--out-md",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional Markdown report path.",
)
@click.option(
    "--build-extractor/--no-build-extractor",
    default=False,
    show_default=True,
    help="Run `pnpm --filter @semsql/extractor-cli build` before probing.",
)
def package_bridge_probe_cmd(
    out_dir: Path,
    semsql_bin: Path,
    out_json: Path | None,
    out_md: Path | None,
    build_extractor: bool,
) -> None:
    """Probe installed-style native -> semsql-extract PATH resolution."""
    report = run_package_bridge_probe(
        out_dir=out_dir,
        semsql_bin=semsql_bin,
        build_extractor=build_extractor,
    )
    rendered = render_package_bridge_probe_markdown(report)
    click.echo(rendered.rstrip())
    if out_json is not None:
        _write_json_report(out_json, report)
    if out_md is not None:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(rendered, encoding="utf-8")
    if report["status"] != "pass":
        raise click.ClickException("package bridge probe failed")


@cli.command("package-launcher-smoke")
@click.option(
    "--out",
    "out_dir",
    type=click.Path(path_type=Path),
    required=True,
    help="Output directory for the fresh package-install smoke.",
)
@click.option(
    "--semsql-bin",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Native semsql binary used as the local release asset.",
)
@click.option(
    "--package-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("packages/semsql-cli"),
    show_default=True,
    help="@semsql/cli package directory to pack and install.",
)
@click.option(
    "--package-manager",
    default="pnpm",
    show_default=True,
    help="Package manager executable used for pack/install/exec.",
)
@click.option(
    "--out-json",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional JSON report path.",
)
@click.option(
    "--out-md",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional Markdown report path.",
)
def package_launcher_smoke_cmd(
    out_dir: Path,
    semsql_bin: Path,
    package_dir: Path,
    package_manager: str,
    out_json: Path | None,
    out_md: Path | None,
) -> None:
    """Smoke a packed @semsql/cli install and manifest binary resolution."""
    report = run_package_launcher_smoke(
        out_dir=out_dir,
        semsql_bin=semsql_bin,
        package_dir=package_dir,
        package_manager=package_manager,
    )
    rendered = render_package_launcher_smoke_markdown(report)
    click.echo(rendered.rstrip())
    if out_json is not None:
        _write_json_report(out_json, report)
    if out_md is not None:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(rendered, encoding="utf-8")
    if report["status"] != "pass":
        raise click.ClickException("package launcher smoke failed")


@cli.command("package-dlx-smoke")
@click.option(
    "--out",
    "out_dir",
    type=click.Path(path_type=Path),
    required=True,
    help="Output directory for the pnpm dlx smoke.",
)
@click.option(
    "--semsql-bin",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Native semsql binary used as the local release asset.",
)
@click.option(
    "--package-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("packages/semsql-cli"),
    show_default=True,
    help="@semsql/cli package directory to pack and install through dlx.",
)
@click.option(
    "--package-manager",
    default="pnpm",
    show_default=True,
    help="Package manager executable used for pack/dlx.",
)
@click.option(
    "--out-json",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional JSON report path.",
)
@click.option(
    "--out-md",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional Markdown report path.",
)
def package_dlx_smoke_cmd(
    out_dir: Path,
    semsql_bin: Path,
    package_dir: Path,
    package_manager: str,
    out_json: Path | None,
    out_md: Path | None,
) -> None:
    """Smoke `pnpm --package <@semsql/cli tarball> dlx semsql`."""
    report = run_package_dlx_smoke(
        out_dir=out_dir,
        semsql_bin=semsql_bin,
        package_dir=package_dir,
        package_manager=package_manager,
    )
    rendered = render_package_dlx_smoke_markdown(report)
    click.echo(rendered.rstrip())
    if out_json is not None:
        _write_json_report(out_json, report)
    if out_md is not None:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(rendered, encoding="utf-8")
    if report["status"] != "pass":
        raise click.ClickException("package dlx smoke failed")


@cli.command("package-registry-smoke")
@click.option(
    "--out",
    "out_dir",
    type=click.Path(path_type=Path),
    required=True,
    help="Output directory for the local-registry npm smoke.",
)
@click.option(
    "--semsql-bin",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Native semsql binary used by @semsql/cli through SEMSQL_BIN.",
)
@click.option(
    "--package-manager",
    default="pnpm",
    show_default=True,
    help="Package manager executable used for dlx.",
)
@click.option(
    "--npm-bin",
    default="npm",
    show_default=True,
    help="npm executable used to publish tarballs to the local registry.",
)
@click.option(
    "--version",
    default="0.1.0-dev",
    show_default=True,
    help="@semsql package version to pack/publish/install.",
)
@click.option(
    "--out-json",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional JSON report path.",
)
@click.option(
    "--out-md",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional Markdown report path.",
)
def package_registry_smoke_cmd(
    out_dir: Path,
    semsql_bin: Path,
    package_manager: str,
    npm_bin: str,
    version: str,
    out_json: Path | None,
    out_md: Path | None,
) -> None:
    """Smoke full @semsql npm stack via a throwaway local registry."""
    report = run_package_registry_smoke(
        out_dir=out_dir,
        semsql_bin=semsql_bin,
        package_manager=package_manager,
        npm_bin=npm_bin,
        version=version,
    )
    rendered = render_package_registry_smoke_markdown(report)
    click.echo(rendered.rstrip())
    if out_json is not None:
        _write_json_report(out_json, report)
    if out_md is not None:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(rendered, encoding="utf-8")
    if report["status"] != "pass":
        raise click.ClickException("package registry smoke failed")


@cli.command("package-public-smoke")
@click.option(
    "--out",
    "out_dir",
    type=click.Path(path_type=Path),
    required=True,
    help="Output directory for the public-registry npm smoke.",
)
@click.option(
    "--version",
    required=True,
    help="Published @semsql package version to install.",
)
@click.option(
    "--semsql-bin",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Optional native semsql binary override. Omit to prove release download.",
)
@click.option(
    "--manifest-url",
    default=None,
    help="Optional semsql-downloads.json URL override for binary download proof.",
)
@click.option(
    "--registry-url",
    default="https://registry.npmjs.org/",
    show_default=True,
    help="npm registry URL to resolve published packages from.",
)
@click.option(
    "--package-manager",
    default="pnpm",
    show_default=True,
    help="Package manager executable used for dlx.",
)
@click.option(
    "--npm-bin",
    default="npm",
    show_default=True,
    help="npm executable used for package version checks.",
)
@click.option(
    "--timeout-seconds",
    default=180,
    show_default=True,
    type=int,
    help="Per-command timeout for npm and pnpm calls.",
)
@click.option(
    "--package-check-retries",
    default=1,
    show_default=True,
    type=int,
    help="Attempts for published package visibility checks.",
)
@click.option(
    "--package-check-delay-seconds",
    default=0.0,
    show_default=True,
    type=float,
    help="Delay between package visibility attempts.",
)
@click.option(
    "--out-json",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional JSON report path.",
)
@click.option(
    "--out-md",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional Markdown report path.",
)
def package_public_smoke_cmd(
    out_dir: Path,
    version: str,
    semsql_bin: Path | None,
    manifest_url: str | None,
    registry_url: str,
    package_manager: str,
    npm_bin: str,
    timeout_seconds: int,
    package_check_retries: int,
    package_check_delay_seconds: float,
    out_json: Path | None,
    out_md: Path | None,
) -> None:
    """Smoke published @semsql packages through the configured npm registry."""
    report = run_package_public_smoke(
        out_dir=out_dir,
        version=version,
        semsql_bin=semsql_bin,
        manifest_url=manifest_url,
        registry_url=registry_url,
        package_manager=package_manager,
        npm_bin=npm_bin,
        timeout_seconds=timeout_seconds,
        package_check_retries=package_check_retries,
        package_check_delay_seconds=package_check_delay_seconds,
    )
    rendered = render_package_public_smoke_markdown(report)
    click.echo(rendered.rstrip())
    if out_json is not None:
        _write_json_report(out_json, report)
    if out_md is not None:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(rendered, encoding="utf-8")
    if report["status"] != "pass":
        raise click.ClickException("package public smoke failed")


@cli.command("real-app-framework-probe")
@click.option(
    "--app-path",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
    help="Real application root to extract.",
)
@click.option(
    "--framework",
    default="auto",
    show_default=True,
    help="Framework adapter to use, e.g. auto or laravel.",
)
@click.option(
    "--db-url",
    required=True,
    help="DB URL used for DB-grounded extraction.",
)
@click.option(
    "--out",
    "out_dir",
    type=click.Path(path_type=Path),
    required=True,
    help="Output directory for graph and probe artifacts.",
)
@click.option(
    "--semsql-bin",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Native semsql binary to exercise.",
)
@click.option(
    "--out-json",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional JSON report path.",
)
@click.option(
    "--out-md",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional Markdown report path.",
)
@click.option(
    "--build-extractor/--no-build-extractor",
    default=False,
    show_default=True,
    help="Run `pnpm --filter @semsql/extractor-cli build` before probing.",
)
@click.option(
    "--min-source-vocab",
    type=int,
    default=1,
    show_default=True,
    help="Minimum ingested source-vocabulary rows required to pass.",
)
@click.option(
    "--query-check-limit",
    type=int,
    default=3,
    show_default=True,
    help="Maximum source-entity count queries to try.",
)
@click.option(
    "--min-query-checks",
    type=int,
    default=1,
    show_default=True,
    help="Minimum source-entity query checks required to pass.",
)
@click.option(
    "--sample-values/--no-sample-values",
    default=False,
    show_default=True,
    help="Allow DB value sampling during extraction.",
)
@click.option(
    "--metric-jsonl",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Optional MetricDefinitionFragment JSONL to ingest with the real app graph.",
)
@click.option(
    "--metric-packet-check-limit",
    type=int,
    default=3,
    show_default=True,
    help="Maximum ingested metric definitions to probe via rejected-query packets.",
)
@click.option(
    "--min-metric-packet-checks",
    type=int,
    default=0,
    show_default=True,
    help="Minimum metric packet checks required to pass.",
)
def real_app_framework_probe_cmd(
    app_path: Path,
    framework: str,
    db_url: str,
    out_dir: Path,
    semsql_bin: Path,
    out_json: Path | None,
    out_md: Path | None,
    build_extractor: bool,
    min_source_vocab: int,
    query_check_limit: int,
    min_query_checks: int,
    sample_values: bool,
    metric_jsonl: Path | None,
    metric_packet_check_limit: int,
    min_metric_packet_checks: int,
) -> None:
    """Probe framework extraction on a real app plus real DB schema."""
    report = run_real_app_framework_probe(
        app_path=app_path,
        framework=framework,
        db_url=db_url,
        out_dir=out_dir,
        semsql_bin=semsql_bin,
        build_extractor=build_extractor,
        min_source_vocab=min_source_vocab,
        query_check_limit=query_check_limit,
        min_query_checks=min_query_checks,
        sample_values=sample_values,
        metric_jsonl=metric_jsonl,
        metric_packet_check_limit=metric_packet_check_limit,
        min_metric_packet_checks=min_metric_packet_checks,
    )
    rendered = render_real_app_framework_probe_markdown(report)
    click.echo(rendered.rstrip())
    if out_json is not None:
        _write_json_report(out_json, report)
    if out_md is not None:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(rendered, encoding="utf-8")
    if report["status"] != "pass":
        raise click.ClickException("real app framework probe failed")


@cli.command("binder-probe")
@click.option(
    "--questions",
    "questions_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Spider/BIRD dev.json or train.json.",
)
@click.option(
    "--db-root",
    "db_root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
    help="Root directory containing per-db SQLite files.",
)
@click.option(
    "--name",
    "suite_name",
    type=click.Choice(["spider", "spider2", "bird"]),
    default="bird",
    show_default=True,
    help="Suite name, matching the spider command.",
)
@click.option("--sample-size", type=click.IntRange(min=1), default=100, show_default=True)
@click.option("--seed", type=int, default=20260530, show_default=True)
@click.option(
    "--current-report-json",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Optional current eval report. Used only to filter/count current mismatches.",
)
@click.option(
    "--only-mismatches",
    is_flag=True,
    help="Sample only examples marked incorrect in --current-report-json.",
)
@click.option("--out-json", type=click.Path(path_type=Path), default=None)
@click.option("--out-md", type=click.Path(path_type=Path), default=None)
def binder_probe_cmd(
    questions_path: Path,
    db_root: Path,
    suite_name: str,
    sample_size: int,
    seed: int,
    current_report_json: Path | None,
    only_mismatches: bool,
    out_json: Path | None,
    out_md: Path | None,
) -> None:
    """Probe whether a schema/value atlas can recover random gold evidence.

    This is a proof harness for the QueryFrame direction. It uses gold SQL only
    after binding to score evidence recovery.
    """
    if only_mismatches and current_report_json is None:
        raise click.UsageError("--only-mismatches requires --current-report-json")
    report = run_binder_probe(
        questions_path=questions_path,
        db_root=db_root,
        suite_name=suite_name,
        sample_size=sample_size,
        seed=seed,
        report_json=current_report_json,
        only_mismatches=only_mismatches,
    )
    if out_json is not None:
        _write_json_report_text(out_json, report.to_json())
    markdown = render_binder_probe_markdown(report)
    if out_md is not None:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(markdown, encoding="utf-8")
    click.echo(markdown)


@cli.command("queryframe-probe")
@click.option(
    "--questions",
    "questions_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Spider/BIRD dev.json or train.json.",
)
@click.option(
    "--db-root",
    "db_root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
    help="Root directory containing per-db SQLite files.",
)
@click.option(
    "--binder-report-json",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Binder proof report defining the frozen sample and proof-ready flags.",
)
@click.option(
    "--current-report-json",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Optional current eval report used to count recovery/regression.",
)
@click.option(
    "--name",
    "suite_name",
    type=click.Choice(["spider", "spider2", "bird"]),
    default="bird",
    show_default=True,
    help="Suite name, matching the spider command.",
)
@click.option(
    "--all-binder-rows",
    is_flag=True,
    help="Evaluate all rows in the binder report instead of proof-ready rows only.",
)
@click.option(
    "--routed-only",
    is_flag=True,
    help="Omit not-routed examples from the written/rendered report.",
)
@click.option(
    "--exec-timeout-seconds",
    type=click.FloatRange(min=0.001),
    default=10.0,
    show_default=True,
)
@click.option("--out-json", type=click.Path(path_type=Path), default=None)
@click.option("--out-md", type=click.Path(path_type=Path), default=None)
def queryframe_probe_cmd(
    questions_path: Path,
    db_root: Path,
    binder_report_json: Path,
    current_report_json: Path | None,
    suite_name: str,
    all_binder_rows: bool,
    routed_only: bool,
    exec_timeout_seconds: float,
    out_json: Path | None,
    out_md: Path | None,
) -> None:
    """Execute the experimental SchemaAtlas + QueryFrame solver probe."""
    report = run_queryframe_probe(
        questions_path=questions_path,
        db_root=db_root,
        suite_name=suite_name,
        binder_report_json=binder_report_json,
        current_report_json=current_report_json,
        proof_ready_only=not all_binder_rows,
        routed_only=routed_only,
        exec_timeout_seconds=exec_timeout_seconds,
    )
    if out_json is not None:
        _write_json_report_text(out_json, report.to_json())
    markdown = render_queryframe_probe_markdown(report)
    if out_md is not None:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(markdown, encoding="utf-8")
    click.echo(markdown)


@cli.command("semantic-atlas-assessment")
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("target/semantic_atlas_assessment"),
    show_default=True,
    help="Working directory for generated practical suites and assessment output.",
)
@click.option(
    "--suite",
    "suites",
    type=click.Choice(["platform", "business"]),
    multiple=True,
    help="Suite(s) to assess. Omit to run both practical suites.",
)
@click.option("--out-json", type=click.Path(path_type=Path), default=None)
@click.option("--out-md", type=click.Path(path_type=Path), default=None)
def semantic_atlas_assessment_cmd(
    out_dir: Path,
    suites: tuple[str, ...],
    out_json: Path | None,
    out_md: Path | None,
) -> None:
    """Compare raw schema/sample evidence with a mini SemanticAtlas layer."""
    selected = tuple(suites or ("platform", "business"))
    report = run_semantic_atlas_assessment(
        out_dir=out_dir,
        suites=selected,  # type: ignore[arg-type]
    )
    json_path = out_json or (out_dir / "semantic_atlas_assessment.json")
    md_path = out_md or (out_dir / "semantic_atlas_assessment.md")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_report(json_path, report)
    rendered = render_semantic_atlas_assessment_markdown(report)
    md_path.write_text(rendered, encoding="utf-8")
    click.echo(rendered)


@cli.command("spider")
@click.pass_context
@click.option(
    "--questions",
    "questions_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Spider/BIRD dev.json or train.json.",
)
@click.option(
    "--db-root",
    "db_root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
    help="Root directory containing per-db SQLite files (Spider's `database/` layout).",
)
@click.option(
    "--semsql-bin",
    type=click.Path(path_type=Path),
    default=Path("target/debug/semsql.exe"),
    help="Path to the compiled `semsql` binary. Falls back to PATH if missing.",
)
@click.option(
    "--graph-cache-dir",
    type=click.Path(path_type=Path),
    default=Path("target/spider_graphs"),
    help="Directory where per-db SemanticGraphs are cached.",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Run only the first N examples. Useful for smoke runs.",
)
@click.option(
    "--db-id",
    "db_ids",
    multiple=True,
    help="Only run examples from this db_id. May be repeated for stratified probes.",
)
@click.option(
    "--index",
    "source_indexes",
    multiple=True,
    type=click.IntRange(min=0),
    help="Only run this zero-based source example index. May be repeated for exact probes.",
)
@click.option(
    "--offset",
    type=click.IntRange(min=0),
    default=0,
    show_default=True,
    help="Skip the first N examples after optional oracle-cache filtering.",
)
@click.option(
    "--name",
    type=click.Choice(["spider", "spider2", "bird"]),
    default="spider",
    help="Suite name — affects gold-SQL field lookup.",
)
@click.option(
    "--report-json",
    type=click.Path(path_type=Path),
    default=None,
    help="If set, write the per-example JSON report to this path.",
)
@click.option(
    "--cascade-manifest",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Optional cascade manifest JSON. Forwarded to every `semsql query` "
    "invocation so Stage 1 + grammar-compile run when the binary was built "
    "with `--features onnx`.",
)
@click.option(
    "--intent-yaml",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Optional intent pattern YAML. Forwarded to every cascade run so "
    "Stage 0b matches against the same library the production deployment "
    "uses.",
)
@click.option(
    "--oracle-cache",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Teacher-cache JSONL used for diagnostic oracle ablations.",
)
@click.option(
    "--oracle-stage2",
    is_flag=True,
    help="Use gold NatSQL skeletons from --oracle-cache when available.",
)
@click.option(
    "--oracle-schema",
    is_flag=True,
    help="Use gold ranked schema slices from --oracle-cache when available.",
)
@click.option(
    "--oracle-stage3",
    is_flag=True,
    help="Use gold slot maps from --oracle-cache when available.",
)
@click.option(
    "--trace-only",
    is_flag=True,
    help="Skip SQL execution scoring; collect cascade/report traces only.",
)
@click.option(
    "--oracle-covered-only",
    is_flag=True,
    help="Only run examples present in --oracle-cache. Useful for trace corpora.",
)
@click.option(
    "--require-stage3-traces",
    is_flag=True,
    help="Fail after writing the report if the run emits zero Stage 3 slot traces.",
)
@click.option(
    "--query-frame-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional directory for per-example `semsql query --query-frame-json` payloads.",
)
@click.option(
    "--query-timeout-seconds",
    type=click.IntRange(min=1),
    default=60,
    show_default=True,
    help="Per-example timeout for `semsql query` subprocesses.",
)
@click.option(
    "--extract-timeout-seconds",
    type=click.IntRange(min=1),
    default=60,
    show_default=True,
    help="Per-DB timeout for `semsql extract` graph-cache subprocesses.",
)
@click.option(
    "--exec-timeout-seconds",
    type=click.FloatRange(min=0.001),
    default=30.0,
    show_default=True,
    help="Per-SQL SQLite execution timeout during exec-acc scoring.",
)
@click.option(
    "--checkpoint-every",
    type=click.IntRange(min=0),
    default=1,
    show_default=True,
    help=(
        "When --report-json is set, atomically rewrite the report every N "
        "completed examples with complete=false. Use 0 to disable mid-run "
        "checkpointing."
    ),
)
@click.option(
    "--progress-every",
    type=click.IntRange(min=0),
    default=25,
    show_default=True,
    help="Print a compact progress line every N completed examples. Use 0 to disable.",
)
def spider_cmd(
    ctx: click.Context,
    questions_path: Path,
    db_root: Path,
    semsql_bin: Path,
    graph_cache_dir: Path,
    limit: int | None,
    db_ids: tuple[str, ...],
    source_indexes: tuple[int, ...],
    offset: int,
    name: str,
    report_json: Path | None,
    cascade_manifest: Path | None,
    intent_yaml: Path | None,
    oracle_cache: Path | None,
    oracle_stage2: bool,
    oracle_schema: bool,
    oracle_stage3: bool,
    trace_only: bool,
    oracle_covered_only: bool,
    require_stage3_traces: bool,
    query_frame_dir: Path | None,
    query_timeout_seconds: int,
    extract_timeout_seconds: int,
    exec_timeout_seconds: float,
    checkpoint_every: int,
    progress_every: int,
) -> None:
    """Run Spider/BIRD evaluation against the cascade."""
    if (oracle_stage2 or oracle_schema or oracle_stage3) and oracle_cache is None:
        raise click.UsageError("--oracle-cache is required with oracle ablation flags")
    if oracle_covered_only and oracle_cache is None:
        raise click.UsageError("--oracle-covered-only requires --oracle-cache")

    suite = SpiderSuite.load(questions_path, db_root, name=name)  # type: ignore[arg-type]
    oracle_records = _load_oracle_cache(oracle_cache) if oracle_cache else {}
    indexed_examples: list[tuple[int, Example]] = list(enumerate(suite.examples))
    source_total = len(indexed_examples)
    selected_source_indexes = tuple(dict.fromkeys(source_indexes))
    if selected_source_indexes:
        selected_indexes = set(selected_source_indexes)
        indexed_examples = [(idx, ex) for idx, ex in indexed_examples if idx in selected_indexes]
    selected_db_ids = tuple(db_id for db_id in db_ids if db_id)
    if selected_db_ids:
        selected_ids = set(selected_db_ids)
        indexed_examples = [(idx, ex) for idx, ex in indexed_examples if ex.db_id in selected_ids]
    if oracle_covered_only:
        indexed_examples = [
            (idx, ex)
            for idx, ex in indexed_examples
            if (ex.db_id, ex.question) in oracle_records
        ]
    filtered_total = len(indexed_examples)
    if offset:
        indexed_examples = indexed_examples[offset:]
    if limit is not None:
        indexed_examples = indexed_examples[:limit]
    selected_total = len(indexed_examples)
    dataset_hash = _sha256_file(questions_path)
    run_started_at_utc = _utc_timestamp()
    semsql_bin_version = _semsql_bin_version(semsql_bin)

    oracle_stats = {
        "stage2_hits": 0,
        "stage2_misses": 0,
        "schema_hits": 0,
        "schema_misses": 0,
        "stage3_hits": 0,
        "stage3_misses": 0,
    }

    def oracle_skeleton_cb(example: Example) -> str | None:
        record = oracle_records.get((example.db_id, example.question))
        skeleton = record.get("natsql_skeleton") if record is not None else None
        if isinstance(skeleton, str) and skeleton:
            oracle_stats["stage2_hits"] += 1
            return skeleton
        oracle_stats["stage2_misses"] += 1
        return None

    def oracle_schema_cb(example: Example) -> str | None:
        record = oracle_records.get((example.db_id, example.question))
        payload = _oracle_schema_payload(record)
        if payload is not None:
            oracle_stats["schema_hits"] += 1
            return json.dumps(payload, separators=(",", ":"))
        oracle_stats["schema_misses"] += 1
        return None

    def oracle_slots_cb(example: Example) -> str | None:
        record = oracle_records.get((example.db_id, example.question))
        slot_map = _oracle_slots_payload(record)
        if slot_map is not None:
            oracle_stats["stage3_hits"] += 1
            return json.dumps(slot_map, separators=(",", ":"))
        oracle_stats["stage3_misses"] += 1
        return None

    # Per-stage tag tracking. The cascade runner emits one tag per
    # query (`stage_0a`, `needs_model`, `error`, `timeout`, ...);
    # we accumulate counts so the summary surfaces a per-stage
    # breakdown of where each example exited.
    stage_tags: dict[tuple[str, str, str, str], str] = {}
    stage_counts: dict[str, int] = {}
    repair_tags: dict[tuple[str, str, str, str], int] = {}
    slot_traces: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
    query_frames: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    cascade_error_details: dict[tuple[str, str, str, str], str] = {}
    query_elapsed_seconds: dict[tuple[str, str, str, str], float] = {}
    query_stdout_bytes: dict[tuple[str, str, str, str], int] = {}
    query_stderr_bytes: dict[tuple[str, str, str, str], int] = {}
    query_timed_out_after_seconds: dict[tuple[str, str, str, str], int] = {}
    query_stage_timings_us: dict[tuple[str, str, str, str], dict[str, int]] = {}
    repair_total: list[int] = [0]  # mutable closure cell — running sum
    slot_trace_total: list[int] = [0]

    def on_stage(ex: Example, tag: str, repair: int = 0) -> None:
        key = _example_key(ex)
        stage_tags[key] = tag
        stage_counts[tag] = stage_counts.get(tag, 0) + 1
        repair_tags[key] = repair
        repair_total[0] += repair

    def on_query_result(ex: Example, result: Any) -> None:
        key = _example_key(ex)
        slots = getattr(result, "stage3_slots", [])
        if slots:
            slot_trace_total[0] += len(slots)
            slot_traces[key] = slots
        frame = getattr(result, "query_frame", None)
        if isinstance(frame, dict):
            query_frames[key] = frame
        error_detail = getattr(result, "error_detail", None)
        if isinstance(error_detail, str) and error_detail:
            cascade_error_details[key] = error_detail
        elapsed_seconds = getattr(result, "elapsed_seconds", None)
        if isinstance(elapsed_seconds, int | float):
            query_elapsed_seconds[key] = float(elapsed_seconds)
        stdout_bytes = getattr(result, "stdout_bytes", None)
        if isinstance(stdout_bytes, int):
            query_stdout_bytes[key] = stdout_bytes
        stderr_bytes = getattr(result, "stderr_bytes", None)
        if isinstance(stderr_bytes, int):
            query_stderr_bytes[key] = stderr_bytes
        timed_out_after_seconds = getattr(result, "timed_out_after_seconds", None)
        if isinstance(timed_out_after_seconds, int):
            query_timed_out_after_seconds[key] = timed_out_after_seconds
        stage_timings_us = getattr(result, "stage_timings_us", None)
        if isinstance(stage_timings_us, dict):
            parsed_timings: dict[str, int] = {}
            for stage_name, elapsed_us in stage_timings_us.items():
                if isinstance(stage_name, str) and isinstance(elapsed_us, int):
                    parsed_timings[stage_name] = elapsed_us
            if parsed_timings:
                query_stage_timings_us[key] = parsed_timings

    predict = make_cascade_predictor(
        semsql_bin=semsql_bin,
        graph_cache_dir=graph_cache_dir,
        on_stage=on_stage,
        on_query_result=on_query_result,
        cascade_manifest=cascade_manifest,
        intent_yaml=intent_yaml,
        oracle_skeleton=oracle_skeleton_cb if oracle_stage2 else None,
        oracle_schema_json=oracle_schema_cb if oracle_schema else None,
        oracle_slots_json=oracle_slots_cb if oracle_stage3 else None,
        query_frame_dir=query_frame_dir,
        extract_timeout_seconds=extract_timeout_seconds,
        query_timeout_seconds=query_timeout_seconds,
    )

    # Per-example records, surfaced when --report-json is set so callers
    # can drill into specific failures without re-running the whole
    # suite.
    records: list[dict[str, object]] = []
    failure_buckets: dict[str, int] = {}
    summary = EvalSummary(suite=suite.name)
    timeout_count = 0

    def write_report(*, complete: bool, interrupted: str | None = None) -> None:
        if report_json is None:
            return
        report_json.parent.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(
            report_json,
            _spider_report_payload(
                ctx=ctx,
                dataset_hash=dataset_hash,
                questions_path=questions_path,
                db_root=db_root,
                semsql_bin=semsql_bin,
                graph_cache_dir=graph_cache_dir,
                cascade_manifest=cascade_manifest,
                intent_yaml=intent_yaml,
                oracle_cache=oracle_cache,
                oracle_records=oracle_records,
                oracle_stage2=oracle_stage2,
                oracle_schema=oracle_schema,
                oracle_stage3=oracle_stage3,
                oracle_stats=oracle_stats,
                limit=limit,
                selected_db_ids=selected_db_ids,
                selected_source_indexes=selected_source_indexes,
                offset=offset,
                source_total=source_total,
                filtered_total=filtered_total,
                selected_total=selected_total,
                oracle_covered_only=oracle_covered_only,
                trace_only=trace_only,
                require_stage3_traces=require_stage3_traces,
                query_frame_dir=query_frame_dir,
                query_timeout_seconds=query_timeout_seconds,
                extract_timeout_seconds=extract_timeout_seconds,
                exec_timeout_seconds=exec_timeout_seconds,
                checkpoint_every=checkpoint_every,
                progress_every=progress_every,
                run_started_at_utc=run_started_at_utc,
                semsql_bin_version=semsql_bin_version,
                summary=summary,
                timeout_count=timeout_count,
                stage_counts=stage_counts,
                failure_buckets=failure_buckets,
                repair_total=repair_total[0],
                slot_trace_total=slot_trace_total[0],
                records=records,
                complete=complete,
                interrupted=interrupted,
                next_index=(
                    indexed_examples[len(records)][0]
                    if len(records) < len(indexed_examples)
                    else None
                ),
            ),
        )

    def predict_logged(source_index: int, example: Example) -> str:
        nonlocal timeout_count
        sql = predict(example)
        key = _example_key(example)
        stage = stage_tags.get(key, "unknown")
        scored = _score_prediction(
            example,
            sql,
            stage_pinned=stage,
            repair_attempts=repair_tags.get(key, 0),
            trace_only=trace_only,
            exec_timeout_seconds=exec_timeout_seconds,
        )
        _apply_record_to_summary(summary, scored)
        if scored["failure_bucket"] == "timeout":
            timeout_count += 1
        bucket = str(scored["failure_bucket"])
        failure_buckets[bucket] = failure_buckets.get(bucket, 0) + 1
        query_frame = query_frames.get(key)
        runtime_query_frame = _runtime_query_frame(query_frame)
        records.append(
            {
                "index": source_index,
                "db_id": example.db_id,
                "question": example.question,
                "gold_sql": example.gold_sql,
                "pred_sql": sql,
                "stage_pinned": stage,
                "stage3_slots": slot_traces.get(key, []),
                "query_frame": query_frame,
                "runtime_query_frame": runtime_query_frame,
                "cascade_error_detail": cascade_error_details.get(key),
                "query_elapsed_seconds": query_elapsed_seconds.get(key),
                "query_stdout_bytes": query_stdout_bytes.get(key),
                "query_stderr_bytes": query_stderr_bytes.get(key),
                "query_timed_out_after_seconds": query_timed_out_after_seconds.get(key),
                "query_stage_timings_us": query_stage_timings_us.get(key),
                **scored,
            }
        )
        return sql

    write_report(complete=False)
    try:
        for completed, (source_index, example) in enumerate(indexed_examples, 1):
            predict_logged(source_index, example)
            if progress_every and completed % progress_every == 0:
                click.echo(
                    "progress: "
                    f"{completed}/{selected_total} "
                    f"last_index={source_index} "
                    f"correct={summary.correct} "
                    f"wrong={summary.wrong} "
                    f"bailed={summary.bailed} "
                    f"errored={summary.errored} "
                    f"timeouts={timeout_count}"
                )
            if report_json is not None and checkpoint_every and completed % checkpoint_every == 0:
                write_report(complete=False)
    except BaseException as exc:
        write_report(complete=False, interrupted=f"{type(exc).__name__}: {exc}")
        raise

    click.echo(
        f"suite={summary.suite}  "
        f"total={summary.total}  "
        f"correct={summary.correct}  "
        f"wrong={summary.wrong}  "
        f"bailed={summary.bailed}  "
        f"errored={summary.errored}  "
        f"exec_acc={summary.exec_acc:.3%}  "
        f"bail_rate={summary.bail_rate:.3%}  "
        f"error_rate={summary.error_rate:.3%}"
    )

    if stage_counts:
        breakdown = "  ".join(f"{tag}={n}" for tag, n in sorted(stage_counts.items()))
        click.echo(f"stages: {breakdown}")

    if report_json is not None:
        write_report(complete=True)
        click.echo(f"per-example report written to {report_json}")

    if require_stage3_traces and slot_trace_total[0] == 0:
        raise click.ClickException(
            "--require-stage3-traces requested, but this run emitted zero Stage 3 "
            "slot traces. Check that the semsql binary was built with "
            "`cargo build -p semsql-cli --features semsql-cli/onnx` and that the selected "
            "examples reach Stage 3."
        )


def _spider_report_payload(
    *,
    ctx: click.Context,
    dataset_hash: str,
    questions_path: Path,
    db_root: Path,
    semsql_bin: Path,
    graph_cache_dir: Path,
    cascade_manifest: Path | None,
    intent_yaml: Path | None,
    oracle_cache: Path | None,
    oracle_records: dict[tuple[str, str], dict[str, Any]],
    oracle_stage2: bool,
    oracle_schema: bool,
    oracle_stage3: bool,
    oracle_stats: dict[str, int],
    limit: int | None,
    selected_db_ids: tuple[str, ...],
    selected_source_indexes: tuple[int, ...],
    offset: int,
    source_total: int,
    filtered_total: int,
    selected_total: int,
    oracle_covered_only: bool,
    trace_only: bool,
    require_stage3_traces: bool,
    query_frame_dir: Path | None,
    query_timeout_seconds: int,
    extract_timeout_seconds: int,
    exec_timeout_seconds: float,
    checkpoint_every: int,
    progress_every: int,
    run_started_at_utc: str,
    semsql_bin_version: dict[str, object],
    summary: EvalSummary,
    timeout_count: int,
    stage_counts: dict[str, int],
    failure_buckets: dict[str, int],
    repair_total: int,
    slot_trace_total: int,
    records: list[dict[str, object]],
    complete: bool,
    interrupted: str | None,
    next_index: int | None,
) -> dict[str, object]:
    return {
        "schema_version": 2,
        "metadata": {
            "provenance": {
                "run_started_at_utc": run_started_at_utc,
                "report_written_at_utc": _utc_timestamp(),
                "semsql_eval_version": SEMSQL_EVAL_VERSION,
                "semsql_bin_version": semsql_bin_version,
            },
            "dataset_hash": dataset_hash,
            "questions_path": str(questions_path),
            "db_root": str(db_root),
            "graph_cache_dir": str(graph_cache_dir),
            "semsql_bin": str(semsql_bin),
            "cascade_manifest": str(cascade_manifest) if cascade_manifest else None,
            "intent_yaml": str(intent_yaml) if intent_yaml else None,
            "oracle": {
                "cache": str(oracle_cache) if oracle_cache else None,
                "cache_records": len(oracle_records),
                "stage2_enabled": oracle_stage2,
                "schema_enabled": oracle_schema,
                "stage3_enabled": oracle_stage3,
                **oracle_stats,
            },
            "limit": limit,
            "db_ids": list(selected_db_ids),
            "indexes": list(selected_source_indexes),
            "offset": offset,
            "source_total": source_total,
            "filtered_total": filtered_total,
            "selected_total": selected_total,
            "oracle_covered_only": oracle_covered_only,
            "trace_only": trace_only,
            "require_stage3_traces": require_stage3_traces,
            "query_frame_dir": str(query_frame_dir) if query_frame_dir else None,
            "query_timeout_seconds": query_timeout_seconds,
            "extract_timeout_seconds": extract_timeout_seconds,
            "exec_timeout_seconds": exec_timeout_seconds,
            "checkpoint_every": checkpoint_every,
            "progress_every": progress_every,
            "run": {
                "complete": complete,
                "interrupted": interrupted,
                "completed": len(records),
                "selected_total": selected_total,
                "last_completed_index": records[-1]["index"] if records else None,
                "next_index": next_index,
            },
            "command": _command_metadata(ctx),
        },
        "summary": {
            "suite": summary.suite,
            "total": summary.total,
            "correct": summary.correct,
            "wrong": summary.wrong,
            "bailed": summary.bailed,
            "errored": summary.errored,
            "timeouts": timeout_count,
            "exec_acc": summary.exec_acc,
            "bail_rate": summary.bail_rate,
            "error_rate": summary.error_rate,
            "stage_breakdown": dict(stage_counts),
            "failure_buckets": dict(sorted(failure_buckets.items())),
            "repair_attempts_total": repair_total,
            "stage3_slot_trace_count": slot_trace_total,
            "query_telemetry": _query_telemetry_summary(records),
            "runtime_query_frame": _runtime_query_frame_summary(records),
        },
        "examples": records,
    }


def _runtime_query_frame(payload: object) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    runtime = payload.get("runtime_query_frame")
    if isinstance(runtime, dict):
        return runtime
    return None


def _runtime_query_frame_summary(records: list[dict[str, object]]) -> dict[str, object]:
    route_reasons: dict[str, int] = {}
    routed_buckets: dict[str, int] = {}
    rejected_buckets: dict[str, int] = {}
    used_buckets: dict[str, int] = {}
    rejected_stage_breakdown: dict[str, int] = {}
    routed = 0
    rejected = 0
    used = 0
    routed_not_used = 0
    missing = 0
    for record in records:
        runtime = _runtime_query_frame(record.get("query_frame"))
        if runtime is None:
            missing += 1
            continue
        reason = str(runtime.get("route_reason") or "unknown")
        route_reasons[reason] = route_reasons.get(reason, 0) + 1
        failure_bucket = str(record.get("failure_bucket") or "unknown")
        if runtime.get("routed") is True:
            routed += 1
            routed_buckets[failure_bucket] = routed_buckets.get(failure_bucket, 0) + 1
            if runtime.get("used_for_final_sql") is True:
                used += 1
                used_buckets[failure_bucket] = used_buckets.get(failure_bucket, 0) + 1
            else:
                routed_not_used += 1
        else:
            rejected += 1
            rejected_buckets[failure_bucket] = rejected_buckets.get(failure_bucket, 0) + 1
            stage = str(record.get("stage_pinned") or "unknown")
            rejected_stage_breakdown[stage] = rejected_stage_breakdown.get(stage, 0) + 1

    return {
        "count": routed + rejected,
        "routed": routed,
        "rejected": rejected,
        "used_for_final_sql": used,
        "routed_not_used": routed_not_used,
        "missing": missing,
        "route_reasons": dict(sorted(route_reasons.items())),
        "routed_failure_buckets": dict(sorted(routed_buckets.items())),
        "used_failure_buckets": dict(sorted(used_buckets.items())),
        "rejected_failure_buckets": dict(sorted(rejected_buckets.items())),
        "rejected_stage_breakdown": dict(sorted(rejected_stage_breakdown.items())),
    }


def _query_telemetry_summary(records: list[dict[str, object]]) -> dict[str, object]:
    elapsed = sorted(
        float(value)
        for record in records
        if isinstance((value := record.get("query_elapsed_seconds")), int | float)
    )
    stdout_total = sum(
        int(value)
        for record in records
        if isinstance((value := record.get("query_stdout_bytes")), int)
    )
    stderr_total = sum(
        int(value)
        for record in records
        if isinstance((value := record.get("query_stderr_bytes")), int)
    )
    timeouts = sum(
        1 for record in records if record.get("query_timed_out_after_seconds") is not None
    )
    stage_timing_totals: dict[str, int] = {}
    for record in records:
        timings = record.get("query_stage_timings_us")
        if not isinstance(timings, dict):
            continue
        for stage_name, elapsed_us in timings.items():
            if isinstance(stage_name, str) and isinstance(elapsed_us, int):
                stage_timing_totals[stage_name] = (
                    stage_timing_totals.get(stage_name, 0) + elapsed_us
                )
    if not elapsed:
        return {
            "count": 0,
            "elapsed_seconds_total": 0.0,
            "elapsed_seconds_avg": None,
            "elapsed_seconds_p95": None,
            "stdout_bytes_total": stdout_total,
            "stderr_bytes_total": stderr_total,
            "subprocess_timeouts": timeouts,
            "stage_timings_us_total": stage_timing_totals,
        }

    p95_index = min(len(elapsed) - 1, max(0, math.ceil(len(elapsed) * 0.95) - 1))
    total = sum(elapsed)
    return {
        "count": len(elapsed),
        "elapsed_seconds_total": total,
        "elapsed_seconds_avg": total / len(elapsed),
        "elapsed_seconds_p95": elapsed[p95_index],
        "stdout_bytes_total": stdout_total,
        "stderr_bytes_total": stderr_total,
        "subprocess_timeouts": timeouts,
        "stage_timings_us_total": stage_timing_totals,
    }


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    for attempt in range(6):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.05 * (2**attempt))


def _read_json_file(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _example_key(ex: Example) -> tuple[str, str, str, str]:
    return (ex.db_id, ex.question, ex.gold_sql, str(ex.db_path))


def _load_oracle_cache(path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    records: dict[tuple[str, str], dict[str, Any]] = {}
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        text = line.strip()
        if not text:
            continue
        try:
            row = json.loads(text)
        except json.JSONDecodeError as exc:
            raise click.ClickException(
                f"invalid oracle cache JSON at {path}:{line_no}: {exc.msg}"
            ) from exc
        if not isinstance(row, dict):
            continue
        db_id = row.get("db_id")
        nl = row.get("nl") or row.get("question")
        if isinstance(db_id, str) and isinstance(nl, str):
            records[(db_id, nl)] = row
    return records


def _oracle_schema_payload(record: dict[str, Any] | None) -> dict[str, object] | None:
    if record is None:
        return None
    ranked_schema = record.get("ranked_schema")
    if not isinstance(ranked_schema, list):
        return None

    entities: list[str] = []
    fields: list[str] = []
    top_score = 0.0
    for item in ranked_schema:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        target = item.get("target")
        if not isinstance(kind, str) or not isinstance(target, str):
            continue
        score = item.get("score")
        if isinstance(score, (int, float)):
            top_score = max(top_score, float(score))
        if kind == "entity":
            _append_unique(entities, _canonicalize_name(target))
        elif kind == "field":
            _append_unique(fields, _canonicalize_field_target(target))

    if not entities and not fields:
        return None
    return {
        "entities": entities,
        "fields": fields,
        "top_score": top_score if top_score > 0.0 else 1.0,
    }


def _oracle_slots_payload(record: dict[str, Any] | None) -> dict[str, object] | None:
    if record is None:
        return None
    slot_map = record.get("slot_map")
    if not isinstance(slot_map, dict):
        return None
    out: dict[str, object] = {}
    for slot, value in slot_map.items():
        if not isinstance(slot, str) or not slot:
            continue
        if isinstance(value, str):
            if slot.startswith("@field"):
                out[slot] = _canonicalize_field_target(value)
            elif slot.startswith("@entity"):
                out[slot] = _canonicalize_name(value)
            else:
                out[slot] = value
        elif isinstance(value, (int, float, bool)):
            out[slot] = value
    return out or None


_NON_CANONICAL_CHARS = re.compile(r"[^a-z0-9]+")


def _canonicalize_field_target(target: str) -> str:
    if "." not in target:
        return _canonicalize_name(target)
    entity, field = target.split(".", 1)
    return f"{_canonicalize_name(entity)}.{_canonicalize_name(field)}"


def _canonicalize_name(raw: str) -> str:
    canonical = _NON_CANONICAL_CHARS.sub("_", raw.strip().lower()).strip("_")
    canonical = re.sub(r"_+", "_", canonical)
    if not canonical:
        return "_"
    if canonical[0].isdigit():
        return f"_{canonical}"
    return canonical


def _append_unique(values: list[str], value: str) -> None:
    if value and value not in values:
        values.append(value)


def _normalise_for_bail(sql: str) -> str:
    return " ".join(sql.split()).strip().rstrip(";").strip().lower()


def _score_prediction(
    example: Example,
    pred_sql: str,
    *,
    stage_pinned: str,
    repair_attempts: int,
    trace_only: bool = False,
    bail_sentinel: str = "SELECT 1",
    exec_timeout_seconds: float | None = 30.0,
) -> dict[str, object]:
    pred_error: str | None = None
    gold_error: str | None = None
    timeout_error: str | None = None
    exec_equal = False
    timeout = stage_pinned == "timeout"
    gold_timeout = False
    pred_timeout = False
    bailed = _normalise_for_bail(pred_sql) == _normalise_for_bail(bail_sentinel)

    if not trace_only and not bailed and not timeout:
        gold = execute(example.db_path, example.gold_sql, timeout_seconds=exec_timeout_seconds)
        pred = execute(example.db_path, pred_sql, timeout_seconds=exec_timeout_seconds)
        gold_error = gold.error
        pred_error = pred.error
        gold_timeout = gold.timed_out
        pred_timeout = pred.timed_out
        if pred_timeout:
            timeout = True
            timeout_error = pred.error
        exec_equal = exec_results_eq(example.gold_sql, gold, pred)

    if exec_equal:
        bucket = "correct"
    elif timeout:
        bucket = "timeout"
    elif gold_timeout:
        bucket = "gold_exec_timeout"
    elif stage_pinned in {
        "stage2_constraint_error",
        "stage2_structural_error",
        "stage4_render_error",
    }:
        bucket = stage_pinned
    elif stage_pinned == "missing_onnx_feature":
        bucket = stage_pinned
    elif stage_pinned == "error":
        bucket = "cascade_error"
    elif trace_only:
        bucket = "not_scored"
    elif bailed:
        bucket = stage_pinned if stage_pinned != "unknown" else "bailed"
    elif gold_error is not None:
        bucket = "gold_exec_error"
    elif pred_error is not None:
        bucket = "pred_exec_error"
    else:
        bucket = "exec_mismatch"

    return {
        "exec_equal": exec_equal,
        "failure_bucket": bucket,
        "timeout": timeout,
        "gold_timeout": gold_timeout,
        "pred_timeout": pred_timeout,
        "timeout_error": timeout_error,
        "error": pred_error,
        "gold_error": gold_error,
        "bailed": bailed,
        "repair_attempts": repair_attempts,
    }


def _apply_record_to_summary(summary: EvalSummary, record: dict[str, object]) -> None:
    summary.total += 1
    if record["exec_equal"]:
        summary.correct += 1
    elif record["failure_bucket"] == "timeout":
        return
    elif record["failure_bucket"] == "gold_exec_timeout":
        return
    elif record["error"] is not None or record["gold_error"] is not None:
        summary.errored += 1
    elif record["failure_bucket"] in {
        "cascade_error",
        "stage2_constraint_error",
        "stage2_structural_error",
        "stage4_render_error",
        "missing_onnx_feature",
    }:
        summary.errored += 1
    elif record["bailed"]:
        summary.bailed += 1


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _command_metadata(ctx: click.Context) -> dict[str, Any]:
    return {
        "program": Path(sys.argv[0]).name,
        "args": list(sys.argv[1:]),
        "command_path": ctx.command_path,
    }


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _semsql_bin_version(semsql_bin: Path) -> dict[str, object]:
    try:
        proc = subprocess.run(
            [str(semsql_bin), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except OSError as exc:
        return {
            "path": str(semsql_bin),
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    except subprocess.TimeoutExpired:
        return {
            "path": str(semsql_bin),
            "ok": False,
            "error": "timeout running --version",
        }
    stdout = proc.stdout.strip()
    stderr = proc.stderr.strip()
    return {
        "path": str(semsql_bin),
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
    }


def _result_provenance() -> dict[str, object]:
    return {
        "generated_at_utc": _utc_timestamp(),
        "semsql_eval_version": SEMSQL_EVAL_VERSION,
    }


def _with_result_provenance(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    provenance = payload.get("provenance")
    if not isinstance(provenance, dict):
        provenance = {}
        payload["provenance"] = provenance
    for key, value in _result_provenance().items():
        provenance.setdefault(key, value)
    return payload


def _write_json_report(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_with_result_provenance(payload), indent=2) + "\n",
        encoding="utf-8",
    )


def _write_json_report_text(path: Path, payload: str) -> None:
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload.rstrip() + "\n", encoding="utf-8")
        return
    _write_json_report(path, parsed)


@cli.command("gate-report")
@click.option(
    "--profile",
    type=click.Choice(["v0.2-bird"]),
    required=True,
    help="Gate profile to enforce.",
)
@click.option(
    "--report-json",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Report emitted by `python -m semsql_eval spider --report-json`.",
)
@click.option(
    "--out",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional Markdown gate summary destination.",
)
def gate_report_cmd(profile: str, report_json: Path, out: Path | None) -> None:
    """Fail closed unless an eval report satisfies a benchmark profile."""
    report = json.loads(report_json.read_text(encoding="utf-8"))
    summary = report.get("summary", {})
    buckets = dict(summary.get("failure_buckets") or {})
    stage_breakdown = dict(summary.get("stage_breakdown") or {})

    required_total = 1534
    exec_acc = float(summary.get("exec_acc") or 0.0)
    total = int(summary.get("total") or 0)
    suite_name = str(summary.get("suite") or "")
    errored = int(summary.get("errored") or 0)
    timeouts = int(
        summary.get("timeouts") or buckets.get("timeout") or stage_breakdown.get("timeout") or 0
    )

    failures: list[str] = []
    if profile == "v0.2-bird":
        if suite_name != "bird":
            failures.append(f"expected suite=bird, got {suite_name or '<missing>'}")
        if total < required_total:
            failures.append(f"expected full BIRD dev total >= {required_total}, got {total}")
        if exec_acc < 0.35:
            failures.append(f"expected exec_acc >= 35.0%, got {exec_acc:.3%}")
        if errored != 0:
            failures.append(f"expected errored == 0, got {errored}")
        if timeouts != 0:
            failures.append(f"expected timeout == 0, got {timeouts}")

    rendered = _render_gate_summary(profile, report_json, summary, buckets, failures)
    click.echo(rendered.rstrip())
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(rendered, encoding="utf-8")
    if failures:
        raise click.ClickException("benchmark gate failed")


def _render_gate_summary(
    profile: str,
    report_json: Path,
    summary: dict[str, object],
    buckets: dict[str, object],
    failures: list[str],
) -> str:
    exec_acc = summary.get("exec_acc") or 0.0
    exec_acc_float = exec_acc if isinstance(exec_acc, (float, int)) else 0.0
    lines = [
        f"# SemanticSQL Gate Report ({profile})",
        "",
        f"- report: `{report_json}`",
        f"- suite: `{summary.get('suite', '<missing>')}`",
        f"- total: `{summary.get('total', 0)}`",
        f"- exec_acc: `{float(exec_acc_float):.3%}`",
        f"- errored: `{summary.get('errored', 0)}`",
        f"- timeouts: `{summary.get('timeouts', buckets.get('timeout', 0))}`",
        "",
        "## Failure Buckets",
        "",
    ]
    if buckets:
        for name, count in sorted(buckets.items()):
            lines.append(f"- `{name}`: {count}")
    else:
        lines.append("- `<none>`: 0")
    lines.extend(["", "## Gate Status", ""])
    if failures:
        lines.append("FAILED")
        lines.extend(f"- {failure}" for failure in failures)
    else:
        lines.append("PASSED")
    lines.append("")
    return "\n".join(lines)


@cli.command("diagnose-report")
@click.option(
    "--report-json",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Report emitted by `python -m semsql_eval spider --report-json`.",
)
@click.option(
    "--out",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional Markdown diagnosis destination.",
)
@click.option(
    "--out-json",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional machine-readable diagnosis JSON destination.",
)
@click.option(
    "--sample-examples",
    type=int,
    default=20,
    show_default=True,
    help="Number of tagged failures to include in the sample section.",
)
@click.option(
    "--fail-on-accepted-wrong-sql",
    is_flag=True,
    help="Exit non-zero if the report contains any emitted final SQL that scored wrong.",
)
def diagnose_report_cmd(
    report_json: Path,
    out: Path | None,
    out_json: Path | None,
    sample_examples: int,
    fail_on_accepted_wrong_sql: bool,
) -> None:
    """Classify gold-vs-pred SQL feature gaps in an eval report."""
    report = diagnose_report(report_json, sample_examples=sample_examples)
    rendered = render_diagnosis_markdown(report)
    click.echo(rendered.rstrip())
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(rendered, encoding="utf-8")
    if out_json is not None:
        _write_json_report_text(out_json, diagnosis_report_to_json(report))
    if fail_on_accepted_wrong_sql and report.product_safety.final_sql_wrong:
        raise click.ClickException(
            "accepted wrong SQL detected: "
            f"{report.product_safety.final_sql_wrong} final SQL mismatches"
        )


@cli.command("ablation-gap")
@click.option(
    "--live-report-json",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Fully live report emitted by `python -m semsql_eval spider`.",
)
@click.option(
    "--oracle-schema-report-json",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Report run with --oracle-schema only.",
)
@click.option(
    "--oracle-stage2-report-json",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Report run with --oracle-stage2 only.",
)
@click.option(
    "--oracle-schema-stage2-report-json",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Report run with --oracle-schema --oracle-stage2.",
)
@click.option(
    "--all-oracle-report-json",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Report run with schema, Stage 2, and Stage 3 oracle overrides.",
)
@click.option(
    "--out",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional Markdown ablation-gap destination.",
)
@click.option(
    "--out-json",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional machine-readable ablation-gap JSON destination.",
)
@click.option(
    "--sample-examples",
    type=int,
    default=20,
    show_default=True,
    help="Number of non-live-correct examples to include.",
)
def ablation_gap_cmd(
    live_report_json: Path,
    oracle_schema_report_json: Path | None,
    oracle_stage2_report_json: Path | None,
    oracle_schema_stage2_report_json: Path | None,
    all_oracle_report_json: Path | None,
    out: Path | None,
    out_json: Path | None,
    sample_examples: int,
) -> None:
    """Compare live end-to-end runs against oracle ablation reports."""
    report = ablation_gap_report(
        live_report_json=live_report_json,
        oracle_schema_report_json=oracle_schema_report_json,
        oracle_stage2_report_json=oracle_stage2_report_json,
        oracle_schema_stage2_report_json=oracle_schema_stage2_report_json,
        all_oracle_report_json=all_oracle_report_json,
        sample_examples=sample_examples,
    )
    rendered = render_ablation_gap_markdown(report)
    click.echo(rendered.rstrip())
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(rendered, encoding="utf-8")
    if out_json is not None:
        _write_json_report_text(out_json, ablation_gap_report_to_json(report))


@cli.command("oracle-gap")
@click.option(
    "--current-report-json",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Live Stage 3 report emitted by `python -m semsql_eval spider`.",
)
@click.option(
    "--oracle-report-json",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="All-oracle upper-bound report over the same example order.",
)
@click.option(
    "--oracle-cache",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Teacher-cache JSONL used to decide oracle coverage.",
)
@click.option(
    "--previous-report-json",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Optional previous live report used to count regressions.",
)
@click.option(
    "--out",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional Markdown oracle-gap destination.",
)
@click.option(
    "--out-json",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional machine-readable oracle-gap JSON destination.",
)
@click.option(
    "--sample-examples",
    type=int,
    default=20,
    show_default=True,
    help="Number of non-both-correct examples to include.",
)
def oracle_gap_cmd(
    current_report_json: Path,
    oracle_report_json: Path,
    oracle_cache: Path,
    previous_report_json: Path | None,
    out: Path | None,
    out_json: Path | None,
    sample_examples: int,
) -> None:
    """Compare live Stage 3 recovery against an all-oracle ceiling."""
    report = oracle_gap_report(
        current_report_json=current_report_json,
        oracle_report_json=oracle_report_json,
        oracle_cache_jsonl=oracle_cache,
        previous_report_json=previous_report_json,
        sample_examples=sample_examples,
    )
    rendered = render_oracle_gap_markdown(report)
    click.echo(rendered.rstrip())
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(rendered, encoding="utf-8")
    if out_json is not None:
        _write_json_report_text(out_json, oracle_gap_report_to_json(report))


@cli.command("build-mini-corpus")
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("target/spider_mini"),
    help="Output directory for the synthetic Spider mini-corpus.",
)
def build_mini_corpus_cmd(out_dir: Path) -> None:
    """Materialise the synthetic Spider 1.0 mini-corpus on disk.

    Use as a CI smoke-test fixture or as a worked example to compare
    against your real Spider tarball layout. The output is a valid
    Spider 1.0 directory:

        <out>/dev.json
        <out>/tables.json
        <out>/database/<db_id>/<db_id>.sqlite

    After building, you can run the full eval against it:

        python -m semsql_eval spider \\
          --questions <out>/dev.json \\
          --db-root <out>/database \\
          --semsql-bin target/debug/semsql.exe
    """
    path = build_corpus(out_dir)
    click.echo(f"mini-corpus written to {path}")


@cli.command("build-queryframe-canary")
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("target/queryframe_canary"),
    help="Output directory for the seeded QueryFrame production canary.",
)
@click.option("--seed", type=int, default=20260601, show_default=True)
@click.option(
    "--variant",
    type=click.Choice(["commerce", "alias", "random_alias"]),
    default="commerce",
    show_default=True,
    help="Schema naming variant to generate.",
)
def build_queryframe_canary_cmd(out_dir: Path, seed: int, variant: str) -> None:
    """Materialise the seeded QueryFrame production canary on disk.

    The output uses the same Spider-shaped layout as the mini-corpus,
    plus queryframe_canary.json with routed and reject-case metadata.
    """
    path = build_queryframe_canary(out_dir, seed=seed, variant=variant)
    click.echo(f"queryframe canary written to {path}")


@cli.command("build-platform-query-suite")
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("target/platform_query_suite"),
    show_default=True,
    help="Output directory for the platform-neutral comparison suite.",
)
@click.option("--out-json", type=click.Path(path_type=Path), default=None)
@click.option("--out-md", type=click.Path(path_type=Path), default=None)
@click.option(
    "--schema-variant",
    type=click.Choice(["canonical", "semantic_alias", "random_alias"]),
    default="canonical",
    show_default=True,
)
@click.option(
    "--schema-alias-seed",
    type=int,
    default=20260605,
    show_default=True,
    help="Seed for random_alias physical table/field naming.",
)
def build_platform_query_suite_cmd(
    out_dir: Path,
    out_json: Path | None,
    out_md: Path | None,
    schema_variant: str,
    schema_alias_seed: int,
) -> None:
    """Build complex NL-to-SQL examples for SemSQL/Dataherald/DB-GPT comparison."""
    suite = build_platform_query_suite(
        out_dir,
        schema_variant=schema_variant,  # type: ignore[arg-type]
        schema_alias_seed=schema_alias_seed,
    )
    if out_json is not None:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(suite, indent=2) + "\n", encoding="utf-8")
    if out_md is not None:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(render_platform_suite_markdown(suite), encoding="utf-8")
    summary = _platform_suite_summary(suite)
    click.echo(
        "platform query suite written to "
        f"{out_dir} "
        f"(cases={summary['cases']}, route={summary['route']}, "
        f"clarify={summary['clarify']}, reject={summary['reject']}, "
        f"known_gap={summary['known_gap']})"
    )


@cli.command("build-business-analytics-suite")
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("target/business_analytics_suite"),
    show_default=True,
    help="Output directory for the practical BI/CRM/growth/sales/ops suite.",
)
@click.option("--out-json", type=click.Path(path_type=Path), default=None)
@click.option("--out-md", type=click.Path(path_type=Path), default=None)
@click.option(
    "--schema-variant",
    type=click.Choice(["canonical", "semantic_alias", "random_alias"]),
    default="canonical",
    show_default=True,
)
@click.option(
    "--schema-alias-seed",
    type=int,
    default=20260605,
    show_default=True,
    help="Seed for random_alias physical table/field naming.",
)
def build_business_analytics_suite_cmd(
    out_dir: Path,
    out_json: Path | None,
    out_md: Path | None,
    schema_variant: str,
    schema_alias_seed: int,
) -> None:
    """Build practical BI, CRM, growth, sales, and operations examples."""
    suite = build_business_analytics_suite(
        out_dir,
        schema_variant=schema_variant,  # type: ignore[arg-type]
        schema_alias_seed=schema_alias_seed,
    )
    if out_json is not None:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(suite, indent=2) + "\n", encoding="utf-8")
    if out_md is not None:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(render_platform_suite_markdown(suite), encoding="utf-8")
    summary = _platform_suite_summary(suite)
    click.echo(
        "business analytics suite written to "
        f"{out_dir} "
        f"(cases={summary['cases']}, route={summary['route']}, "
        f"clarify={summary['clarify']}, reject={summary['reject']}, "
        f"known_gap={summary['known_gap']})"
    )


@cli.command("pathway-benchmark")
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("target/pathway_decision_benchmark"),
    show_default=True,
    help="Output directory for generated suites, traces, and reports.",
)
@click.option(
    "--suite",
    "suites",
    type=click.Choice(["platform", "business"]),
    multiple=True,
    default=("platform", "business"),
    show_default=True,
    help="Suite to include. Repeat to choose a subset.",
)
@click.option(
    "--semsql-bin",
    type=click.Path(path_type=Path),
    default=Path("target/debug/semsql.exe"),
    help="Path to the compiled `semsql` binary. Falls back to PATH if missing.",
)
@click.option(
    "--graph-cache-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Optional graph-cache directory. Defaults under --out.",
)
@click.option(
    "--cascade-manifest",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Optional cascade manifest forwarded to every `semsql query` call.",
)
@click.option(
    "--intent-yaml",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Optional intent library YAML forwarded to every `semsql query` call.",
)
@click.option(
    "--paraphrase-variants-per-route",
    type=click.IntRange(min=0),
    default=0,
    show_default=True,
    help="Add seeded route-question paraphrases per route case.",
)
@click.option(
    "--paraphrase-seed",
    type=int,
    default=17,
    show_default=True,
    help="Seed for deterministic route-question paraphrases.",
)
@click.option(
    "--schema-variant",
    type=click.Choice(["canonical", "semantic_alias", "random_alias"]),
    default="canonical",
    show_default=True,
    help="Schema fixture variant. Alias variants rename physical tables/fields and add sidecars.",
)
@click.option(
    "--schema-alias-seed",
    type=int,
    default=20260605,
    show_default=True,
    help="Seed for random_alias physical table/field naming.",
)
@click.option(
    "--query-timeout-seconds",
    type=click.IntRange(min=1),
    default=30,
    show_default=True,
)
@click.option(
    "--extract-timeout-seconds",
    type=click.IntRange(min=1),
    default=60,
    show_default=True,
)
@click.option(
    "--exec-timeout-seconds",
    type=click.FloatRange(min=0.001),
    default=10.0,
    show_default=True,
)
@click.option("--out-json", type=click.Path(path_type=Path), default=None)
@click.option("--out-md", type=click.Path(path_type=Path), default=None)
@click.option(
    "--strict",
    is_flag=True,
    help="Exit non-zero on accepted wrong SQL or missing route/reject coverage.",
)
def pathway_benchmark_cmd(
    out_dir: Path,
    suites: tuple[str, ...],
    semsql_bin: Path,
    graph_cache_dir: Path | None,
    cascade_manifest: Path | None,
    intent_yaml: Path | None,
    paraphrase_variants_per_route: int,
    paraphrase_seed: int,
    schema_variant: str,
    schema_alias_seed: int,
    query_timeout_seconds: int,
    extract_timeout_seconds: int,
    exec_timeout_seconds: float,
    out_json: Path | None,
    out_md: Path | None,
    strict: bool,
) -> None:
    """Run the Stage 3/4 pathway decision benchmark."""
    selected = tuple(dict.fromkeys(suites))
    report = run_pathway_benchmark(
        out_dir=out_dir,
        semsql_bin=semsql_bin,
        suites=selected,  # type: ignore[arg-type]
        graph_cache_dir=graph_cache_dir,
        cascade_manifest=cascade_manifest,
        intent_yaml=intent_yaml,
        paraphrase_variants_per_route=paraphrase_variants_per_route,
        paraphrase_seed=paraphrase_seed,
        schema_variant=schema_variant,  # type: ignore[arg-type]
        schema_alias_seed=schema_alias_seed,
        query_timeout_seconds=query_timeout_seconds,
        extract_timeout_seconds=extract_timeout_seconds,
        exec_timeout_seconds=exec_timeout_seconds,
    )
    rendered = render_pathway_benchmark_markdown(report)
    json_path = out_json or (out_dir / "pathway_benchmark.json")
    md_path = out_md or (out_dir / "pathway_benchmark.md")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_report(json_path, report)
    md_path.write_text(rendered, encoding="utf-8")
    click.echo(rendered.rstrip())
    failures = pathway_product_gate_failures(report)
    if strict and failures:
        raise click.ClickException(
            "pathway product gate failed: " + ", ".join(failures)
        )


@cli.command("production-readiness-report")
@click.option(
    "--pathway-report-json",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Existing pathway-benchmark JSON report.",
)
@click.option(
    "--queryframe-canary-json",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Existing queryframe-canary or queryframe-canary-suite JSON report.",
)
@click.option(
    "--llm-safety-json",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Existing llm-resolution-safety-gate JSON report.",
)
@click.option(
    "--realdb-json",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    multiple=True,
    help="Existing realdb schema/fallback probe JSON report. Repeatable.",
)
@click.option(
    "--framework-json",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    multiple=True,
    help="Existing framework extraction/bridge probe JSON report. Repeatable.",
)
@click.option(
    "--package-public-smoke-json",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Existing package-public-smoke JSON report.",
)
@click.option(
    "--require-public-package-smoke",
    is_flag=True,
    help="Deprecated compatibility flag; public package smoke is always required for release-candidate status.",
)
@click.option("--out-json", type=click.Path(path_type=Path), default=None)
@click.option("--out-md", type=click.Path(path_type=Path), default=None)
@click.option(
    "--strict",
    is_flag=True,
    help="Exit non-zero unless core evidence is pilot-safe.",
)
@click.option(
    "--strict-release",
    is_flag=True,
    help="Exit non-zero unless all required release surfaces pass.",
)
def production_readiness_report_cmd(
    pathway_report_json: Path | None,
    queryframe_canary_json: Path | None,
    llm_safety_json: Path | None,
    realdb_json: tuple[Path, ...],
    framework_json: tuple[Path, ...],
    package_public_smoke_json: Path | None,
    require_public_package_smoke: bool,
    out_json: Path | None,
    out_md: Path | None,
    strict: bool,
    strict_release: bool,
) -> None:
    """Build a compact promotion stoplight from existing evidence JSON."""
    report = build_production_readiness_report(
        pathway_report=(
            load_json_report(pathway_report_json)
            if pathway_report_json is not None
            else None
        ),
        queryframe_canary_report=(
            load_json_report(queryframe_canary_json)
            if queryframe_canary_json is not None
            else None
        ),
        llm_safety_report=(
            load_json_report(llm_safety_json) if llm_safety_json is not None else None
        ),
        realdb_reports=[load_json_report(path) for path in realdb_json],
        framework_reports=[load_json_report(path) for path in framework_json],
        package_public_smoke_report=(
            load_json_report(package_public_smoke_json)
            if package_public_smoke_json is not None
            else None
        ),
        require_public_package_smoke=require_public_package_smoke,
    )
    report["provenance"] = {
        "generated_at_utc": _utc_timestamp(),
        "semsql_eval_version": SEMSQL_EVAL_VERSION,
        "input_reports": {
            "pathway": str(pathway_report_json) if pathway_report_json else None,
            "queryframe_canary": (
                str(queryframe_canary_json) if queryframe_canary_json else None
            ),
            "llm_safety": str(llm_safety_json) if llm_safety_json else None,
            "realdb": [str(path) for path in realdb_json],
            "framework": [str(path) for path in framework_json],
            "package_public_smoke": (
                str(package_public_smoke_json)
                if package_public_smoke_json
                else None
            ),
        },
    }
    rendered = render_production_readiness_markdown(report)
    if out_json is not None:
        _write_json_report(out_json, report)
    if out_md is not None:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(rendered, encoding="utf-8")
    click.echo(rendered.rstrip())
    summary = report["summary"]
    if strict_release and summary["release_candidate"] is not True:
        raise click.ClickException("production readiness release gate failed")
    if strict and summary["pilot_safe"] is not True:
        raise click.ClickException("production readiness pilot gate failed")


def _platform_suite_summary(suite: dict[str, Any]) -> dict[str, int]:
    counts = {"cases": 0, "route": 0, "clarify": 0, "reject": 0, "known_gap": 0}
    cases = suite.get("cases", [])
    if not isinstance(cases, list):
        return counts
    counts["cases"] = len(cases)
    for case in cases:
        if not isinstance(case, dict):
            continue
        disposition = case.get("disposition")
        if isinstance(disposition, str) and disposition in counts:
            counts[disposition] += 1
    return counts


@cli.command("queryframe-canary")
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("target/queryframe_canary"),
    help="Output directory for the generated canary corpus and frames.",
)
@click.option("--seed", type=int, default=20260601, show_default=True)
@click.option(
    "--variant",
    type=click.Choice(["commerce", "alias", "random_alias"]),
    default="commerce",
    show_default=True,
    help="Schema naming variant to generate and run.",
)
@click.option(
    "--semsql-bin",
    type=click.Path(path_type=Path),
    default=Path("target/debug/semsql.exe"),
    help="Path to the compiled `semsql` binary. Falls back to PATH if missing.",
)
@click.option(
    "--graph-cache-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Optional graph-cache directory. Defaults under --out.",
)
@click.option(
    "--cascade-manifest",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Optional cascade manifest forwarded to every `semsql query` call.",
)
@click.option(
    "--intent-yaml",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Optional intent library YAML forwarded to every `semsql query` call.",
)
@click.option(
    "--query-timeout-seconds",
    type=click.IntRange(min=1),
    default=30,
    show_default=True,
)
@click.option(
    "--extract-timeout-seconds",
    type=click.IntRange(min=1),
    default=60,
    show_default=True,
)
@click.option(
    "--exec-timeout-seconds",
    type=click.FloatRange(min=0.001),
    default=10.0,
    show_default=True,
)
@click.option("--out-json", type=click.Path(path_type=Path), default=None)
@click.option("--out-md", type=click.Path(path_type=Path), default=None)
@click.option(
    "--strict",
    is_flag=True,
    help="Exit non-zero unless every routed case is correct and every reject fails closed.",
)
def queryframe_canary_cmd(
    out_dir: Path,
    seed: int,
    variant: str,
    semsql_bin: Path,
    graph_cache_dir: Path | None,
    cascade_manifest: Path | None,
    intent_yaml: Path | None,
    query_timeout_seconds: int,
    extract_timeout_seconds: int,
    exec_timeout_seconds: float,
    out_json: Path | None,
    out_md: Path | None,
    strict: bool,
) -> None:
    """Run the seeded QueryFrame production canary end-to-end."""
    report = run_queryframe_canary(
        out_dir=out_dir,
        seed=seed,
        variant=variant,
        semsql_bin=semsql_bin,
        graph_cache_dir=graph_cache_dir,
        cascade_manifest=cascade_manifest,
        intent_yaml=intent_yaml,
        query_timeout_seconds=query_timeout_seconds,
        extract_timeout_seconds=extract_timeout_seconds,
        exec_timeout_seconds=exec_timeout_seconds,
    )
    rendered = render_queryframe_canary_markdown(report)
    if out_json is not None:
        _write_json_report(out_json, report)
    if out_md is not None:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(rendered, encoding="utf-8")
    click.echo(rendered.rstrip())
    if strict and not report["summary"]["pass"]:
        raise click.ClickException("queryframe canary failed")


@cli.command("queryframe-canary-postgres")
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("target/queryframe_canary_postgres"),
    help="Output directory for the generated Postgres canary corpus and frames.",
)
@click.option("--seed", type=int, default=20260601, show_default=True)
@click.option(
    "--variant",
    type=click.Choice(["commerce", "alias", "random_alias"]),
    default="commerce",
    show_default=True,
    help="Schema naming variant to generate and run.",
)
@click.option(
    "--db-url",
    type=str,
    default=None,
    help="Live throwaway Postgres URL. Defaults to SEMSQL_POSTGRES_CANARY_URL.",
)
@click.option(
    "--semsql-bin",
    type=click.Path(path_type=Path),
    default=Path("target/debug/semsql.exe"),
    help="Path to the compiled `semsql` binary. Falls back to PATH if missing.",
)
@click.option(
    "--graph-cache-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Optional graph-cache directory. Defaults under --out.",
)
@click.option(
    "--cascade-manifest",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Optional cascade manifest forwarded to every `semsql query` call.",
)
@click.option(
    "--intent-yaml",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Optional intent library YAML forwarded to every `semsql query` call.",
)
@click.option(
    "--query-timeout-seconds",
    type=click.IntRange(min=1),
    default=30,
    show_default=True,
)
@click.option(
    "--extract-timeout-seconds",
    type=click.IntRange(min=1),
    default=60,
    show_default=True,
)
@click.option(
    "--exec-timeout-seconds",
    type=click.FloatRange(min=0.001),
    default=10.0,
    show_default=True,
)
@click.option("--keep-schema", is_flag=True, help="Leave the canary schema in Postgres.")
@click.option("--out-json", type=click.Path(path_type=Path), default=None)
@click.option("--out-md", type=click.Path(path_type=Path), default=None)
@click.option(
    "--strict",
    is_flag=True,
    help="Exit non-zero unless the live Postgres canary passes.",
)
def queryframe_canary_postgres_cmd(
    out_dir: Path,
    seed: int,
    variant: str,
    db_url: str | None,
    semsql_bin: Path,
    graph_cache_dir: Path | None,
    cascade_manifest: Path | None,
    intent_yaml: Path | None,
    query_timeout_seconds: int,
    extract_timeout_seconds: int,
    exec_timeout_seconds: float,
    keep_schema: bool,
    out_json: Path | None,
    out_md: Path | None,
    strict: bool,
) -> None:
    """Run the QueryFrame canary against a live Postgres database.

    Without ``--db-url`` or ``SEMSQL_POSTGRES_CANARY_URL`` this command writes
    the fixture and reports a structured skip.
    """
    report = run_queryframe_postgres_canary(
        out_dir=out_dir,
        seed=seed,
        variant=variant,
        db_url=db_url,
        semsql_bin=semsql_bin,
        graph_cache_dir=graph_cache_dir,
        cascade_manifest=cascade_manifest,
        intent_yaml=intent_yaml,
        query_timeout_seconds=query_timeout_seconds,
        extract_timeout_seconds=extract_timeout_seconds,
        exec_timeout_seconds=exec_timeout_seconds,
        keep_schema=keep_schema,
    )
    rendered = render_queryframe_postgres_canary_markdown(report)
    if out_json is not None:
        _write_json_report(out_json, report)
    if out_md is not None:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(rendered, encoding="utf-8")
    click.echo(rendered.rstrip())
    if strict and not report["summary"]["pass"]:
        raise click.ClickException("queryframe postgres canary did not pass")


@cli.command("queryframe-canary-mysql")
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("target/queryframe_canary_mysql"),
    help="Output directory for the generated MySQL/MariaDB canary corpus and frames.",
)
@click.option("--seed", type=int, default=20260601, show_default=True)
@click.option(
    "--variant",
    type=click.Choice(["commerce", "alias", "random_alias"]),
    default="commerce",
    show_default=True,
    help="Schema naming variant to generate and run.",
)
@click.option(
    "--db-url",
    type=str,
    default=None,
    help="Live MySQL/MariaDB server URL. Defaults to SEMSQL_MYSQL_CANARY_URL.",
)
@click.option(
    "--semsql-bin",
    type=click.Path(path_type=Path),
    default=Path("target/debug/semsql.exe"),
    help="Path to the compiled `semsql` binary. Falls back to PATH if missing.",
)
@click.option(
    "--graph-cache-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Optional graph-cache directory. Defaults under --out.",
)
@click.option(
    "--cascade-manifest",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Optional cascade manifest forwarded to every `semsql query` call.",
)
@click.option(
    "--intent-yaml",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Optional intent library YAML forwarded to every `semsql query` call.",
)
@click.option(
    "--query-timeout-seconds",
    type=click.IntRange(min=1),
    default=30,
    show_default=True,
)
@click.option(
    "--extract-timeout-seconds",
    type=click.IntRange(min=1),
    default=60,
    show_default=True,
)
@click.option(
    "--exec-timeout-seconds",
    type=click.FloatRange(min=0.001),
    default=10.0,
    show_default=True,
)
@click.option("--keep-database", is_flag=True, help="Leave the canary database in MySQL/MariaDB.")
@click.option("--out-json", type=click.Path(path_type=Path), default=None)
@click.option("--out-md", type=click.Path(path_type=Path), default=None)
@click.option(
    "--strict",
    is_flag=True,
    help="Exit non-zero unless the live MySQL/MariaDB canary passes.",
)
def queryframe_canary_mysql_cmd(
    out_dir: Path,
    seed: int,
    variant: str,
    db_url: str | None,
    semsql_bin: Path,
    graph_cache_dir: Path | None,
    cascade_manifest: Path | None,
    intent_yaml: Path | None,
    query_timeout_seconds: int,
    extract_timeout_seconds: int,
    exec_timeout_seconds: float,
    keep_database: bool,
    out_json: Path | None,
    out_md: Path | None,
    strict: bool,
) -> None:
    """Run the QueryFrame canary against live MySQL/MariaDB."""
    report = run_queryframe_mysql_canary(
        out_dir=out_dir,
        seed=seed,
        variant=variant,
        db_url=db_url,
        semsql_bin=semsql_bin,
        graph_cache_dir=graph_cache_dir,
        cascade_manifest=cascade_manifest,
        intent_yaml=intent_yaml,
        query_timeout_seconds=query_timeout_seconds,
        extract_timeout_seconds=extract_timeout_seconds,
        exec_timeout_seconds=exec_timeout_seconds,
        keep_database=keep_database,
    )
    rendered = render_queryframe_mysql_canary_markdown(report)
    if out_json is not None:
        _write_json_report(out_json, report)
    if out_md is not None:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(rendered, encoding="utf-8")
    click.echo(rendered.rstrip())
    if strict and not report["summary"]["pass"]:
        raise click.ClickException("queryframe mysql canary did not pass")


@cli.command("realdb-schema-probe-mysql")
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("target/realdb_schema_probe_mysql"),
    help="Output directory for schema-only graphs and transient probe files.",
)
@click.option("--seed", type=int, default=20260601, show_default=True)
@click.option(
    "--db-url",
    type=str,
    default=None,
    help="Live MySQL/MariaDB server URL. Defaults to SEMSQL_MYSQL_PROBE_URL.",
)
@click.option(
    "--database",
    type=str,
    default=None,
    help="Database to probe. Defaults to the URL path, then a seeded random non-system DB.",
)
@click.option(
    "--sample-size",
    type=click.IntRange(min=1),
    default=10,
    show_default=True,
    help="Number of generated table-count questions.",
)
@click.option(
    "--unsafe-prompt-count",
    type=click.IntRange(min=0),
    default=2,
    show_default=True,
    help="Number of sensitive row-returning prompts that must fail closed.",
)
@click.option(
    "--analytics-probe-count",
    type=click.IntRange(min=0),
    default=0,
    show_default=True,
    help=(
        "Number of optional schema-derived aggregate/date/group questions to "
        "diagnose. They are reported separately from the required safety contract."
    ),
)
@click.option(
    "--semsql-bin",
    type=click.Path(path_type=Path),
    default=Path("target/debug/semsql.exe"),
    help="Path to the compiled `semsql` binary. Falls back to PATH if missing.",
)
@click.option(
    "--graph-cache-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Optional graph-cache directory. Defaults under --out.",
)
@click.option(
    "--query-timeout-seconds",
    type=click.IntRange(min=1),
    default=30,
    show_default=True,
)
@click.option(
    "--extract-timeout-seconds",
    type=click.IntRange(min=1),
    default=60,
    show_default=True,
)
@click.option(
    "--exec-timeout-seconds",
    type=click.FloatRange(min=0.001),
    default=10.0,
    show_default=True,
)
@click.option(
    "--include-generated",
    is_flag=True,
    help="Allow random selection of generated semsql_/test_ databases.",
)
@click.option("--out-json", type=click.Path(path_type=Path), default=None)
@click.option("--out-md", type=click.Path(path_type=Path), default=None)
@click.option(
    "--strict",
    is_flag=True,
    help="Exit non-zero unless the real-DB schema-only probe passes.",
)
def realdb_schema_probe_mysql_cmd(
    out_dir: Path,
    seed: int,
    db_url: str | None,
    database: str | None,
    sample_size: int,
    unsafe_prompt_count: int,
    analytics_probe_count: int,
    semsql_bin: Path,
    graph_cache_dir: Path | None,
    query_timeout_seconds: int,
    extract_timeout_seconds: int,
    exec_timeout_seconds: float,
    include_generated: bool,
    out_json: Path | None,
    out_md: Path | None,
    strict: bool,
) -> None:
    """Probe an existing MySQL/MariaDB database safely.

    The probe extracts schema only, executes only exact table-count routes,
    discards count results, and expects sensitive row-returning prompts to fail
    closed.
    """
    report = run_mysql_realdb_schema_probe(
        out_dir=out_dir,
        seed=seed,
        db_url=db_url,
        database=database,
        sample_size=sample_size,
        unsafe_prompt_count=unsafe_prompt_count,
        analytics_probe_count=analytics_probe_count,
        semsql_bin=semsql_bin,
        graph_cache_dir=graph_cache_dir,
        query_timeout_seconds=query_timeout_seconds,
        extract_timeout_seconds=extract_timeout_seconds,
        exec_timeout_seconds=exec_timeout_seconds,
        include_generated=include_generated,
    )
    rendered = render_mysql_realdb_schema_probe_markdown(report)
    if out_json is not None:
        _write_json_report(out_json, report)
    if out_md is not None:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(rendered, encoding="utf-8")
    click.echo(rendered.rstrip())
    if strict and not report["summary"]["pass"]:
        raise click.ClickException("realdb schema probe mysql did not pass")


@cli.command("realdb-schema-probe-mysql-suite")
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("target/realdb_schema_probe_mysql_suite"),
    help="Output directory for schema-only graphs and transient probe files.",
)
@click.option(
    "--seed",
    "seeds",
    type=int,
    multiple=True,
    default=(20260601, 20260602, 20260603),
    show_default=True,
    help="Seeded DB draw to include. Repeat for multiple random draws.",
)
@click.option(
    "--db-url",
    type=str,
    default=None,
    help="Live MySQL/MariaDB server URL. Defaults to SEMSQL_MYSQL_PROBE_URL.",
)
@click.option(
    "--database",
    type=str,
    default=None,
    help="Database to probe for every seed. Defaults to seeded random non-system DBs.",
)
@click.option(
    "--sample-size",
    type=click.IntRange(min=1),
    default=10,
    show_default=True,
    help="Number of generated table-count questions per run.",
)
@click.option(
    "--unsafe-prompt-count",
    type=click.IntRange(min=0),
    default=2,
    show_default=True,
    help="Number of sensitive row-returning prompts per run that must fail closed.",
)
@click.option(
    "--analytics-probe-count",
    type=click.IntRange(min=0),
    default=0,
    show_default=True,
    help=(
        "Number of optional schema-derived aggregate/date/group questions per "
        "run to diagnose."
    ),
)
@click.option(
    "--semsql-bin",
    type=click.Path(path_type=Path),
    default=Path("target/debug/semsql.exe"),
    help="Path to the compiled `semsql` binary. Falls back to PATH if missing.",
)
@click.option(
    "--graph-cache-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Optional graph-cache directory. Defaults under --out.",
)
@click.option(
    "--query-timeout-seconds",
    type=click.IntRange(min=1),
    default=30,
    show_default=True,
)
@click.option(
    "--extract-timeout-seconds",
    type=click.IntRange(min=1),
    default=60,
    show_default=True,
)
@click.option(
    "--exec-timeout-seconds",
    type=click.FloatRange(min=0.001),
    default=10.0,
    show_default=True,
)
@click.option(
    "--include-generated",
    is_flag=True,
    help="Allow random selection of generated semsql_/test_ databases.",
)
@click.option("--out-json", type=click.Path(path_type=Path), default=None)
@click.option("--out-md", type=click.Path(path_type=Path), default=None)
@click.option(
    "--strict",
    is_flag=True,
    help="Exit non-zero unless every non-skipped real-DB schema-only probe passes.",
)
def realdb_schema_probe_mysql_suite_cmd(
    out_dir: Path,
    seeds: tuple[int, ...],
    db_url: str | None,
    database: str | None,
    sample_size: int,
    unsafe_prompt_count: int,
    analytics_probe_count: int,
    semsql_bin: Path,
    graph_cache_dir: Path | None,
    query_timeout_seconds: int,
    extract_timeout_seconds: int,
    exec_timeout_seconds: float,
    include_generated: bool,
    out_json: Path | None,
    out_md: Path | None,
    strict: bool,
) -> None:
    """Probe several existing MySQL/MariaDB databases safely.

    Each run uses schema-only extraction, executes only exact table-count
    routes, discards count results, and expects sensitive row-returning prompts
    to fail closed.
    """
    report = run_mysql_realdb_schema_probe_suite(
        out_dir=out_dir,
        seeds=list(seeds),
        db_url=db_url,
        database=database,
        sample_size=sample_size,
        unsafe_prompt_count=unsafe_prompt_count,
        analytics_probe_count=analytics_probe_count,
        semsql_bin=semsql_bin,
        graph_cache_dir=graph_cache_dir,
        query_timeout_seconds=query_timeout_seconds,
        extract_timeout_seconds=extract_timeout_seconds,
        exec_timeout_seconds=exec_timeout_seconds,
        include_generated=include_generated,
    )
    rendered = render_mysql_realdb_schema_probe_suite_markdown(report)
    if out_json is not None:
        _write_json_report(out_json, report)
    if out_md is not None:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(rendered, encoding="utf-8")
    click.echo(rendered.rstrip())
    if strict and not report["summary"]["pass"]:
        raise click.ClickException("realdb schema probe mysql suite did not pass")


@cli.command("realdb-schema-probe-postgres")
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("target/realdb_schema_probe_postgres"),
    help="Output directory for schema-only graph and transient probe files.",
)
@click.option("--seed", type=int, default=20260601, show_default=True)
@click.option(
    "--db-url",
    type=str,
    default=None,
    help="Live Postgres server URL. Defaults to SEMSQL_POSTGRES_PROBE_URL.",
)
@click.option(
    "--database",
    type=str,
    default=None,
    help="Database to probe. Defaults to the URL path/current database.",
)
@click.option(
    "--sample-size",
    type=click.IntRange(min=1),
    default=10,
    show_default=True,
    help="Number of generated table-count questions.",
)
@click.option(
    "--unsafe-prompt-count",
    type=click.IntRange(min=0),
    default=2,
    show_default=True,
    help="Number of sensitive row-returning prompts that must fail closed.",
)
@click.option(
    "--analytics-probe-count",
    type=click.IntRange(min=0),
    default=0,
    show_default=True,
    help="Number of optional schema-derived aggregate/date/group questions to diagnose.",
)
@click.option(
    "--semsql-bin",
    type=click.Path(path_type=Path),
    default=Path("target/debug/semsql.exe"),
    help="Path to the compiled `semsql` binary. Falls back to PATH if missing.",
)
@click.option(
    "--graph-cache-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Optional graph-cache directory. Defaults under --out.",
)
@click.option(
    "--query-timeout-seconds",
    type=click.IntRange(min=1),
    default=30,
    show_default=True,
)
@click.option(
    "--extract-timeout-seconds",
    type=click.IntRange(min=1),
    default=60,
    show_default=True,
)
@click.option(
    "--exec-timeout-seconds",
    type=click.FloatRange(min=0.001),
    default=10.0,
    show_default=True,
)
@click.option(
    "--include-generated",
    is_flag=True,
    help="Allow probing generated semsql_/test_ databases.",
)
@click.option("--out-json", type=click.Path(path_type=Path), default=None)
@click.option("--out-md", type=click.Path(path_type=Path), default=None)
@click.option(
    "--strict",
    is_flag=True,
    help="Exit non-zero unless the real-DB schema-only probe passes.",
)
def realdb_schema_probe_postgres_cmd(
    out_dir: Path,
    seed: int,
    db_url: str | None,
    database: str | None,
    sample_size: int,
    unsafe_prompt_count: int,
    analytics_probe_count: int,
    semsql_bin: Path,
    graph_cache_dir: Path | None,
    query_timeout_seconds: int,
    extract_timeout_seconds: int,
    exec_timeout_seconds: float,
    include_generated: bool,
    out_json: Path | None,
    out_md: Path | None,
    strict: bool,
) -> None:
    """Probe an existing Postgres database safely."""
    report = run_postgres_realdb_schema_probe(
        out_dir=out_dir,
        seed=seed,
        db_url=db_url,
        database=database,
        sample_size=sample_size,
        unsafe_prompt_count=unsafe_prompt_count,
        analytics_probe_count=analytics_probe_count,
        semsql_bin=semsql_bin,
        graph_cache_dir=graph_cache_dir,
        query_timeout_seconds=query_timeout_seconds,
        extract_timeout_seconds=extract_timeout_seconds,
        exec_timeout_seconds=exec_timeout_seconds,
        include_generated=include_generated,
    )
    rendered = render_postgres_realdb_schema_probe_markdown(report)
    if out_json is not None:
        _write_json_report(out_json, report)
    if out_md is not None:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(rendered, encoding="utf-8")
    click.echo(rendered.rstrip())
    if strict and not report["summary"]["pass"]:
        raise click.ClickException("realdb schema probe postgres did not pass")


@cli.command("realdb-schema-probe-postgres-suite")
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("target/realdb_schema_probe_postgres_suite"),
    help="Output directory for schema-only graphs and transient probe files.",
)
@click.option(
    "--seed",
    "seeds",
    type=int,
    multiple=True,
    default=(20260601, 20260602, 20260603),
    show_default=True,
    help="Seeded draw to include. Repeat for multiple runs.",
)
@click.option(
    "--db-url",
    type=str,
    default=None,
    help="Live Postgres server URL. Defaults to SEMSQL_POSTGRES_PROBE_URL.",
)
@click.option(
    "--database",
    type=str,
    default=None,
    help="Database to probe for every seed. Defaults to the URL path/current database.",
)
@click.option(
    "--sample-size",
    type=click.IntRange(min=1),
    default=10,
    show_default=True,
    help="Number of generated table-count questions per run.",
)
@click.option(
    "--unsafe-prompt-count",
    type=click.IntRange(min=0),
    default=2,
    show_default=True,
    help="Number of sensitive row-returning prompts per run that must fail closed.",
)
@click.option(
    "--analytics-probe-count",
    type=click.IntRange(min=0),
    default=0,
    show_default=True,
    help="Number of optional schema-derived aggregate/date/group questions per run.",
)
@click.option(
    "--semsql-bin",
    type=click.Path(path_type=Path),
    default=Path("target/debug/semsql.exe"),
    help="Path to the compiled `semsql` binary. Falls back to PATH if missing.",
)
@click.option(
    "--graph-cache-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Optional graph-cache directory. Defaults under --out.",
)
@click.option(
    "--query-timeout-seconds",
    type=click.IntRange(min=1),
    default=30,
    show_default=True,
)
@click.option(
    "--extract-timeout-seconds",
    type=click.IntRange(min=1),
    default=60,
    show_default=True,
)
@click.option(
    "--exec-timeout-seconds",
    type=click.FloatRange(min=0.001),
    default=10.0,
    show_default=True,
)
@click.option(
    "--include-generated",
    is_flag=True,
    help="Allow probing generated semsql_/test_ databases.",
)
@click.option("--out-json", type=click.Path(path_type=Path), default=None)
@click.option("--out-md", type=click.Path(path_type=Path), default=None)
@click.option(
    "--strict",
    is_flag=True,
    help="Exit non-zero unless every non-skipped real-DB schema-only probe passes.",
)
def realdb_schema_probe_postgres_suite_cmd(
    out_dir: Path,
    seeds: tuple[int, ...],
    db_url: str | None,
    database: str | None,
    sample_size: int,
    unsafe_prompt_count: int,
    analytics_probe_count: int,
    semsql_bin: Path,
    graph_cache_dir: Path | None,
    query_timeout_seconds: int,
    extract_timeout_seconds: int,
    exec_timeout_seconds: float,
    include_generated: bool,
    out_json: Path | None,
    out_md: Path | None,
    strict: bool,
) -> None:
    """Probe several Postgres runs safely."""
    report = run_postgres_realdb_schema_probe_suite(
        out_dir=out_dir,
        seeds=list(seeds),
        db_url=db_url,
        database=database,
        sample_size=sample_size,
        unsafe_prompt_count=unsafe_prompt_count,
        analytics_probe_count=analytics_probe_count,
        semsql_bin=semsql_bin,
        graph_cache_dir=graph_cache_dir,
        query_timeout_seconds=query_timeout_seconds,
        extract_timeout_seconds=extract_timeout_seconds,
        exec_timeout_seconds=exec_timeout_seconds,
        include_generated=include_generated,
    )
    rendered = render_postgres_realdb_schema_probe_suite_markdown(report)
    if out_json is not None:
        _write_json_report(out_json, report)
    if out_md is not None:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(rendered, encoding="utf-8")
    click.echo(rendered.rstrip())
    if strict and not report["summary"]["pass"]:
        raise click.ClickException("realdb schema probe postgres suite did not pass")


@cli.command("realdb-typed-fallback-mysql")
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("target/realdb_typed_fallback_mysql"),
    help="Output directory for schema-only graph and per-question fallback artifacts.",
)
@click.option("--seed", type=int, default=20260604, show_default=True)
@click.option(
    "--db-url",
    type=str,
    default=None,
    help="Live MySQL/MariaDB server URL. Defaults to SEMSQL_MYSQL_PROBE_URL.",
)
@click.option(
    "--database",
    type=str,
    default=None,
    help="Database to probe. Defaults to the URL path, then a seeded random non-system DB.",
)
@click.option(
    "--probe-count",
    type=click.IntRange(min=1),
    default=3,
    show_default=True,
    help="Number of schema-derived fallback prompts per selected family.",
)
@click.option(
    "--family",
    "families",
    type=click.Choice(
        [
            "rate",
            "grouped_avg",
            "multi_series_grouped_avg",
            "filtered_grouped_avg",
            "value_filtered_grouped_avg",
            "joined_filtered_grouped_avg",
            "multi_joined_filtered_grouped_avg",
        ]
    ),
    multiple=True,
    default=("rate",),
    show_default=True,
    help="Probe family to generate. Repeat to combine families.",
)
@click.option(
    "--provider",
    type=click.Choice(["none", *TYPED_PROVIDER_CHOICES]),
    default="none",
    show_default=True,
    help="Optional typed proposal provider for local rejections.",
)
@click.option(
    "--provider-base-url",
    type=str,
    default=None,
    help="Base URL for --provider openai-compatible. Appends /chat/completions unless already present.",
)
@click.option(
    "--provider-api-key-env",
    type=str,
    default=None,
    help="Environment variable containing the provider API key for --provider openai-compatible.",
)
@click.option(
    "--model",
    type=str,
    default=None,
    help="Provider model. Defaults are provider-specific.",
)
@click.option(
    "--semsql-bin",
    type=click.Path(path_type=Path),
    default=Path("target/debug/semsql.exe"),
    help="Path to the compiled `semsql` binary. Falls back to PATH if missing.",
)
@click.option(
    "--graph-cache-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Optional graph-cache directory. Defaults under --out.",
)
@click.option(
    "--timeout-seconds",
    type=click.IntRange(min=1),
    default=30,
    show_default=True,
    help="Timeout for each local `semsql query` subprocess.",
)
@click.option(
    "--extract-timeout-seconds",
    type=click.IntRange(min=1),
    default=60,
    show_default=True,
)
@click.option(
    "--exec-timeout-seconds",
    type=click.FloatRange(min=0.001),
    default=30.0,
    show_default=True,
)
@click.option(
    "--include-sample-values",
    is_flag=True,
    help=(
        "Extract bounded non-redacted DB sample_values and include them in "
        "fallback packets. Required for sample-backed value-filter probes."
    ),
)
@click.option(
    "--include-generated",
    is_flag=True,
    help="Allow random selection of generated semsql_/test_ databases.",
)
@click.option("--out-json", type=click.Path(path_type=Path), default=None)
@click.option("--out-md", type=click.Path(path_type=Path), default=None)
@click.option(
    "--strict",
    is_flag=True,
    help="Exit non-zero unless every selected fallback probe validates and executes.",
)
def realdb_typed_fallback_mysql_cmd(
    out_dir: Path,
    seed: int,
    db_url: str | None,
    database: str | None,
    probe_count: int,
    families: tuple[str, ...],
    provider: str,
    provider_base_url: str | None,
    provider_api_key_env: str | None,
    model: str | None,
    semsql_bin: Path,
    graph_cache_dir: Path | None,
    timeout_seconds: int,
    extract_timeout_seconds: int,
    exec_timeout_seconds: float,
    include_sample_values: bool,
    include_generated: bool,
    out_json: Path | None,
    out_md: Path | None,
    strict: bool,
) -> None:
    """Probe typed fallback over schema-derived real MySQL/MariaDB prompts."""
    report = _run_realdb_typed_fallback_mysql(
        out_dir=out_dir,
        seed=seed,
        db_url=db_url,
        database=database,
        probe_count=probe_count,
        families=families,
        provider=provider,
        provider_base_url=provider_base_url,
        provider_api_key_env=provider_api_key_env,
        model=model,
        semsql_bin=semsql_bin,
        graph_cache_dir=graph_cache_dir,
        timeout_seconds=timeout_seconds,
        extract_timeout_seconds=extract_timeout_seconds,
        exec_timeout_seconds=exec_timeout_seconds,
        include_sample_values=include_sample_values,
        include_generated=include_generated,
    )
    rendered = _render_realdb_typed_fallback_mysql_markdown(report)
    if out_json is not None:
        _write_json_report(out_json, report)
    if out_md is not None:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(rendered, encoding="utf-8")
    click.echo(rendered.rstrip())
    if strict and not report["summary"]["pass"]:
        raise click.ClickException("realdb typed fallback mysql probe did not pass")


@cli.command("realdb-typed-fallback-mysql-suite")
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("target/realdb_typed_fallback_mysql_suite"),
    help="Output directory for seeded real-DB typed fallback runs.",
)
@click.option(
    "--seed",
    "seeds",
    type=int,
    multiple=True,
    default=(20260604, 20260605, 20260606),
    show_default=True,
    help="Seeded DB draw to include. Repeat for multiple random draws.",
)
@click.option(
    "--db-url",
    type=str,
    default=None,
    help="Live MySQL/MariaDB server URL. Defaults to SEMSQL_MYSQL_PROBE_URL.",
)
@click.option(
    "--database",
    type=str,
    default=None,
    help="Database to probe for every seed. Defaults to seeded random non-system DBs.",
)
@click.option(
    "--probe-count",
    type=click.IntRange(min=1),
    default=3,
    show_default=True,
    help="Number of schema-derived fallback prompts per selected family per run.",
)
@click.option(
    "--family",
    "families",
    type=click.Choice(
        [
            "rate",
            "grouped_avg",
            "multi_series_grouped_avg",
            "filtered_grouped_avg",
            "value_filtered_grouped_avg",
            "joined_filtered_grouped_avg",
            "multi_joined_filtered_grouped_avg",
        ]
    ),
    multiple=True,
    default=("grouped_avg",),
    show_default=True,
    help="Probe family to generate. Repeat to combine families.",
)
@click.option(
    "--provider",
    type=click.Choice(["none", *TYPED_PROVIDER_CHOICES]),
    default="none",
    show_default=True,
    help="Optional typed proposal provider for local rejections.",
)
@click.option(
    "--provider-base-url",
    type=str,
    default=None,
    help="Base URL for --provider openai-compatible. Appends /chat/completions unless already present.",
)
@click.option(
    "--provider-api-key-env",
    type=str,
    default=None,
    help="Environment variable containing the provider API key for --provider openai-compatible.",
)
@click.option(
    "--model",
    type=str,
    default=None,
    help="Provider model. Defaults are provider-specific.",
)
@click.option(
    "--semsql-bin",
    type=click.Path(path_type=Path),
    default=Path("target/debug/semsql.exe"),
    help="Path to the compiled `semsql` binary. Falls back to PATH if missing.",
)
@click.option(
    "--graph-cache-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Optional graph-cache directory. Defaults under --out.",
)
@click.option(
    "--timeout-seconds",
    type=click.IntRange(min=1),
    default=30,
    show_default=True,
    help="Timeout for each local `semsql query` subprocess.",
)
@click.option(
    "--extract-timeout-seconds",
    type=click.IntRange(min=1),
    default=60,
    show_default=True,
)
@click.option(
    "--exec-timeout-seconds",
    type=click.FloatRange(min=0.001),
    default=30.0,
    show_default=True,
)
@click.option(
    "--include-sample-values",
    is_flag=True,
    help=(
        "Extract bounded non-redacted DB sample_values and include them in "
        "fallback packets. Required for sample-backed value-filter probes."
    ),
)
@click.option(
    "--include-generated",
    is_flag=True,
    help="Allow random selection of generated semsql_/test_ databases.",
)
@click.option("--out-json", type=click.Path(path_type=Path), default=None)
@click.option("--out-md", type=click.Path(path_type=Path), default=None)
@click.option(
    "--strict",
    is_flag=True,
    help=(
        "Exit non-zero unless at least one run passes and every non-skipped "
        "run passes."
    ),
)
def realdb_typed_fallback_mysql_suite_cmd(
    out_dir: Path,
    seeds: tuple[int, ...],
    db_url: str | None,
    database: str | None,
    probe_count: int,
    families: tuple[str, ...],
    provider: str,
    provider_base_url: str | None,
    provider_api_key_env: str | None,
    model: str | None,
    semsql_bin: Path,
    graph_cache_dir: Path | None,
    timeout_seconds: int,
    extract_timeout_seconds: int,
    exec_timeout_seconds: float,
    include_sample_values: bool,
    include_generated: bool,
    out_json: Path | None,
    out_md: Path | None,
    strict: bool,
) -> None:
    """Probe typed fallback across several existing MySQL/MariaDB databases."""
    report = _run_realdb_typed_fallback_mysql_suite(
        out_dir=out_dir,
        seeds=list(seeds),
        db_url=db_url,
        database=database,
        probe_count=probe_count,
        families=families,
        provider=provider,
        provider_base_url=provider_base_url,
        provider_api_key_env=provider_api_key_env,
        model=model,
        semsql_bin=semsql_bin,
        graph_cache_dir=graph_cache_dir,
        timeout_seconds=timeout_seconds,
        extract_timeout_seconds=extract_timeout_seconds,
        exec_timeout_seconds=exec_timeout_seconds,
        include_sample_values=include_sample_values,
        include_generated=include_generated,
    )
    rendered = _render_realdb_typed_fallback_mysql_suite_markdown(report)
    if out_json is not None:
        _write_json_report(out_json, report)
    if out_md is not None:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(rendered, encoding="utf-8")
    click.echo(rendered.rstrip())
    if strict and not report["summary"]["pass"]:
        raise click.ClickException("realdb typed fallback mysql suite did not pass")


@cli.command("realdb-typed-fallback-postgres")
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("target/realdb_typed_fallback_postgres"),
    help="Output directory for schema-only graph and per-question fallback artifacts.",
)
@click.option("--seed", type=int, default=20260604, show_default=True)
@click.option(
    "--db-url",
    type=str,
    default=None,
    help="Live Postgres server URL. Defaults to SEMSQL_POSTGRES_PROBE_URL.",
)
@click.option(
    "--database",
    type=str,
    default=None,
    help="Database to probe. Defaults to the URL path/current database.",
)
@click.option(
    "--probe-count",
    type=click.IntRange(min=1),
    default=3,
    show_default=True,
    help="Number of schema-derived fallback prompts per selected family.",
)
@click.option(
    "--family",
    "families",
    type=click.Choice(
        [
            "rate",
            "grouped_avg",
            "multi_series_grouped_avg",
            "filtered_grouped_avg",
            "value_filtered_grouped_avg",
            "joined_filtered_grouped_avg",
            "multi_joined_filtered_grouped_avg",
        ]
    ),
    multiple=True,
    default=("rate",),
    show_default=True,
    help="Probe family to generate. Repeat to combine families.",
)
@click.option(
    "--provider",
    type=click.Choice(["none", *TYPED_PROVIDER_CHOICES]),
    default="none",
    show_default=True,
    help="Optional typed proposal provider for local rejections.",
)
@click.option(
    "--provider-base-url",
    type=str,
    default=None,
    help="Base URL for --provider openai-compatible. Appends /chat/completions unless already present.",
)
@click.option(
    "--provider-api-key-env",
    type=str,
    default=None,
    help="Environment variable containing the provider API key for --provider openai-compatible.",
)
@click.option(
    "--model",
    type=str,
    default=None,
    help="Provider model. Defaults are provider-specific.",
)
@click.option(
    "--semsql-bin",
    type=click.Path(path_type=Path),
    default=Path("target/debug/semsql.exe"),
    help="Path to the compiled `semsql` binary. Falls back to PATH if missing.",
)
@click.option(
    "--graph-cache-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Optional graph-cache directory. Defaults under --out.",
)
@click.option(
    "--timeout-seconds",
    type=click.IntRange(min=1),
    default=30,
    show_default=True,
    help="Timeout for each local `semsql query` subprocess.",
)
@click.option(
    "--extract-timeout-seconds",
    type=click.IntRange(min=1),
    default=60,
    show_default=True,
)
@click.option(
    "--exec-timeout-seconds",
    type=click.FloatRange(min=0.001),
    default=30.0,
    show_default=True,
)
@click.option(
    "--include-sample-values",
    is_flag=True,
    help=(
        "Extract bounded non-redacted DB sample_values and include them in "
        "fallback packets. Required for sample-backed value-filter probes."
    ),
)
@click.option(
    "--include-generated",
    is_flag=True,
    help="Allow probing generated semsql_/test_ databases.",
)
@click.option("--out-json", type=click.Path(path_type=Path), default=None)
@click.option("--out-md", type=click.Path(path_type=Path), default=None)
@click.option(
    "--strict",
    is_flag=True,
    help="Exit non-zero unless every selected fallback probe validates and executes.",
)
def realdb_typed_fallback_postgres_cmd(
    out_dir: Path,
    seed: int,
    db_url: str | None,
    database: str | None,
    probe_count: int,
    families: tuple[str, ...],
    provider: str,
    provider_base_url: str | None,
    provider_api_key_env: str | None,
    model: str | None,
    semsql_bin: Path,
    graph_cache_dir: Path | None,
    timeout_seconds: int,
    extract_timeout_seconds: int,
    exec_timeout_seconds: float,
    include_sample_values: bool,
    include_generated: bool,
    out_json: Path | None,
    out_md: Path | None,
    strict: bool,
) -> None:
    """Probe typed fallback over schema-derived real Postgres prompts."""
    report = _run_realdb_typed_fallback_mysql(
        out_dir=out_dir,
        seed=seed,
        db_url=db_url,
        database=database,
        probe_count=probe_count,
        families=families,
        provider=provider,
        provider_base_url=provider_base_url,
        provider_api_key_env=provider_api_key_env,
        model=model,
        semsql_bin=semsql_bin,
        graph_cache_dir=graph_cache_dir,
        timeout_seconds=timeout_seconds,
        extract_timeout_seconds=extract_timeout_seconds,
        exec_timeout_seconds=exec_timeout_seconds,
        include_sample_values=include_sample_values,
        include_generated=include_generated,
        engine="postgres",
    )
    rendered = _render_realdb_typed_fallback_postgres_markdown(report)
    if out_json is not None:
        _write_json_report(out_json, report)
    if out_md is not None:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(rendered, encoding="utf-8")
    click.echo(rendered.rstrip())
    if strict and not report["summary"]["pass"]:
        raise click.ClickException("realdb typed fallback postgres probe did not pass")


@cli.command("realdb-typed-fallback-postgres-suite")
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("target/realdb_typed_fallback_postgres_suite"),
    help="Output directory for seeded real-DB typed fallback runs.",
)
@click.option(
    "--seed",
    "seeds",
    type=int,
    multiple=True,
    default=(20260604, 20260605, 20260606),
    show_default=True,
    help="Seeded draw to include. Repeat for multiple runs.",
)
@click.option(
    "--db-url",
    type=str,
    default=None,
    help="Live Postgres server URL. Defaults to SEMSQL_POSTGRES_PROBE_URL.",
)
@click.option(
    "--database",
    type=str,
    default=None,
    help="Database to probe for every seed. Defaults to the URL path/current database.",
)
@click.option(
    "--probe-count",
    type=click.IntRange(min=1),
    default=3,
    show_default=True,
    help="Number of schema-derived fallback prompts per selected family per run.",
)
@click.option(
    "--family",
    "families",
    type=click.Choice(
        [
            "rate",
            "grouped_avg",
            "multi_series_grouped_avg",
            "filtered_grouped_avg",
            "value_filtered_grouped_avg",
            "joined_filtered_grouped_avg",
            "multi_joined_filtered_grouped_avg",
        ]
    ),
    multiple=True,
    default=("rate",),
    show_default=True,
    help="Probe family to generate. Repeat to combine families.",
)
@click.option(
    "--provider",
    type=click.Choice(["none", *TYPED_PROVIDER_CHOICES]),
    default="none",
    show_default=True,
    help="Optional typed proposal provider for local rejections.",
)
@click.option(
    "--provider-base-url",
    type=str,
    default=None,
    help="Base URL for --provider openai-compatible. Appends /chat/completions unless already present.",
)
@click.option(
    "--provider-api-key-env",
    type=str,
    default=None,
    help="Environment variable containing the provider API key for --provider openai-compatible.",
)
@click.option(
    "--model",
    type=str,
    default=None,
    help="Provider model. Defaults are provider-specific.",
)
@click.option(
    "--semsql-bin",
    type=click.Path(path_type=Path),
    default=Path("target/debug/semsql.exe"),
    help="Path to the compiled `semsql` binary. Falls back to PATH if missing.",
)
@click.option(
    "--graph-cache-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Optional graph-cache directory. Defaults under --out.",
)
@click.option(
    "--timeout-seconds",
    type=click.IntRange(min=1),
    default=30,
    show_default=True,
    help="Timeout for each local `semsql query` subprocess.",
)
@click.option(
    "--extract-timeout-seconds",
    type=click.IntRange(min=1),
    default=60,
    show_default=True,
)
@click.option(
    "--exec-timeout-seconds",
    type=click.FloatRange(min=0.001),
    default=30.0,
    show_default=True,
)
@click.option(
    "--include-sample-values",
    is_flag=True,
    help=(
        "Extract bounded non-redacted DB sample_values and include them in "
        "fallback packets. Required for sample-backed value-filter probes."
    ),
)
@click.option(
    "--include-generated",
    is_flag=True,
    help="Allow probing generated semsql_/test_ databases.",
)
@click.option("--out-json", type=click.Path(path_type=Path), default=None)
@click.option("--out-md", type=click.Path(path_type=Path), default=None)
@click.option(
    "--strict",
    is_flag=True,
    help=(
        "Exit non-zero unless at least one run passes and every non-skipped "
        "run passes."
    ),
)
def realdb_typed_fallback_postgres_suite_cmd(
    out_dir: Path,
    seeds: tuple[int, ...],
    db_url: str | None,
    database: str | None,
    probe_count: int,
    families: tuple[str, ...],
    provider: str,
    provider_base_url: str | None,
    provider_api_key_env: str | None,
    model: str | None,
    semsql_bin: Path,
    graph_cache_dir: Path | None,
    timeout_seconds: int,
    extract_timeout_seconds: int,
    exec_timeout_seconds: float,
    include_sample_values: bool,
    include_generated: bool,
    out_json: Path | None,
    out_md: Path | None,
    strict: bool,
) -> None:
    """Probe typed fallback across several existing Postgres runs."""
    report = _run_realdb_typed_fallback_mysql_suite(
        out_dir=out_dir,
        seeds=list(seeds),
        db_url=db_url,
        database=database,
        probe_count=probe_count,
        families=families,
        provider=provider,
        provider_base_url=provider_base_url,
        provider_api_key_env=provider_api_key_env,
        model=model,
        semsql_bin=semsql_bin,
        graph_cache_dir=graph_cache_dir,
        timeout_seconds=timeout_seconds,
        extract_timeout_seconds=extract_timeout_seconds,
        exec_timeout_seconds=exec_timeout_seconds,
        include_sample_values=include_sample_values,
        include_generated=include_generated,
        engine="postgres",
    )
    rendered = _render_realdb_typed_fallback_postgres_suite_markdown(report)
    if out_json is not None:
        _write_json_report(out_json, report)
    if out_md is not None:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(rendered, encoding="utf-8")
    click.echo(rendered.rstrip())
    if strict and not report["summary"]["pass"]:
        raise click.ClickException("realdb typed fallback postgres suite did not pass")


def _run_realdb_typed_fallback_mysql_suite(
    *,
    out_dir: Path,
    seeds: list[int] | tuple[int, ...],
    db_url: str | None,
    database: str | None,
    probe_count: int,
    families: tuple[str, ...],
    provider: str,
    provider_base_url: str | None,
    provider_api_key_env: str | None,
    model: str | None,
    semsql_bin: Path,
    graph_cache_dir: Path | None,
    timeout_seconds: int,
    extract_timeout_seconds: int,
    exec_timeout_seconds: float,
    include_sample_values: bool,
    include_generated: bool,
    engine: str = "mysql",
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    runs: list[dict[str, Any]] = []
    for seed in seeds:
        run_out_dir = out_dir / f"seed-{seed}"
        run_graph_cache_dir = (
            graph_cache_dir / f"seed-{seed}" if graph_cache_dir is not None else None
        )
        runs.append(
            _run_realdb_typed_fallback_mysql(
                out_dir=run_out_dir,
                seed=seed,
                db_url=db_url,
                database=database,
                probe_count=probe_count,
                families=families,
                provider=provider,
                provider_base_url=provider_base_url,
                provider_api_key_env=provider_api_key_env,
                model=model,
                semsql_bin=semsql_bin,
                graph_cache_dir=run_graph_cache_dir,
                timeout_seconds=timeout_seconds,
                extract_timeout_seconds=extract_timeout_seconds,
                exec_timeout_seconds=exec_timeout_seconds,
                include_sample_values=include_sample_values,
                include_generated=include_generated,
                engine=engine,
            )
        )
    summary = _summarize_realdb_typed_fallback_suite_runs(runs)
    status = "pass" if summary["pass"] else "fail"
    if summary["run_passed"] == 0 and summary["run_failed_or_error"] == 0:
        status = "skipped"
    return {
        "schema_version": 1,
        "engine": engine,
        "status": status,
        "seeds": list(seeds),
        "database": database,
        "database_url_redacted": redact_db_url(db_url) if db_url else None,
        "out_dir": str(out_dir),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "safety_mode": (
            "schema-only extraction; "
            f"{'bounded non-redacted sample values included' if include_sample_values else 'no sample values'}; "
            "provider may propose "
            "typed plans only; SQL is locally rendered/validated and executed "
            "read-only with row values discarded"
        ),
        "provider": provider,
        "model": model,
        "families": list(families),
        "summary": summary,
        "runs": runs,
    }


def _summarize_realdb_typed_fallback_suite_runs(
    runs: list[dict[str, Any]],
) -> dict[str, Any]:
    run_total = len(runs)
    run_passed = sum(1 for run in runs if run.get("status") == "pass")
    run_skipped = sum(1 for run in runs if run.get("status") == "skipped")
    run_failed_or_error = sum(
        1 for run in runs if run.get("status") in {"fail", "error"}
    )
    questions = sum(int(run["summary"].get("questions", 0)) for run in runs)
    selected = sum(int(run["summary"].get("selected", 0)) for run in runs)
    local_selected = sum(int(run["summary"].get("local_selected", 0)) for run in runs)
    typed_fallback_selected = sum(
        int(run["summary"].get("typed_fallback_selected", 0)) for run in runs
    )
    provider_call_count = sum(
        int(run["summary"].get("provider_call_count", 0)) for run in runs
    )
    execution_ok = sum(int(run["summary"].get("execution_ok", 0)) for run in runs)
    expected_field_matches = sum(
        int(run["summary"].get("expected_field_matches", 0)) for run in runs
    )
    result_shape_ok = sum(
        int(run["summary"].get("result_shape_ok", 0)) for run in runs
    )
    rows_retained_cases = sum(
        int(run["summary"].get("rows_retained_cases", 0)) for run in runs
    )
    sample_value_rows = sum(
        int(run["summary"].get("sample_value_rows", 0)) for run in runs
    )
    provider_errors = sum(int(run["summary"].get("provider_errors", 0)) for run in runs)
    render_errors = sum(int(run["summary"].get("render_errors", 0)) for run in runs)
    ok = sum(int(run["summary"].get("ok", 0)) for run in runs)
    kind_counts: Counter[str] = Counter()
    result_shape_counts: Counter[str] = Counter()
    for run in runs:
        for kind, count in dict(
            run["summary"].get("expected_kind_counts", {})
        ).items():
            kind_counts[str(kind)] += int(count)
        for kind, count in dict(run["summary"].get("result_shape_counts", {})).items():
            result_shape_counts[str(kind)] += int(count)
    packet_schema_evidence = _summarize_realdb_suite_packet_schema_evidence(runs)
    packet_schema_evidence_ok = all(
        int(payload.get("missing_total") or 0) == 0
        for payload in packet_schema_evidence.values()
        if isinstance(payload, dict)
    )
    provider_readiness = _summarize_realdb_suite_provider_readiness(runs)
    databases = sorted(
        {
            str(run.get("database"))
            for run in runs
            if run.get("database") not in {None, ""}
        }
    )
    return {
        "run_total": run_total,
        "run_passed": run_passed,
        "run_skipped": run_skipped,
        "run_failed_or_error": run_failed_or_error,
        "databases": databases,
        "questions": questions,
        "selected": selected,
        "typed_fallback_selected": typed_fallback_selected,
        "local_selected": local_selected,
        "provider_call_count": provider_call_count,
        "provider_errors": provider_errors,
        "render_errors": render_errors,
        "execution_ok": execution_ok,
        "expected_field_matches": expected_field_matches,
        "expected_kind_counts": dict(kind_counts),
        "result_shape_ok": result_shape_ok,
        "result_shape_counts": dict(result_shape_counts),
        "rows_retained_cases": rows_retained_cases,
        "ok": ok,
        "sample_value_rows": sample_value_rows,
        "packet_schema_evidence": packet_schema_evidence,
        "packet_schema_evidence_ok": packet_schema_evidence_ok,
        "provider_readiness": provider_readiness,
        "pass": (
            run_passed > 0
            and run_failed_or_error == 0
            and result_shape_ok == questions
            and packet_schema_evidence_ok
        ),
        "skipped": run_passed == 0 and run_failed_or_error == 0,
    }


def _summarize_realdb_suite_provider_readiness(
    runs: list[dict[str, Any]],
) -> dict[str, Any]:
    checked_records = 0
    configured_records = 0
    unconfigured_records = 0
    missing_env: Counter[str] = Counter()
    providers: Counter[str] = Counter()
    for run in runs:
        run_summary = run.get("summary")
        if not isinstance(run_summary, dict):
            continue
        readiness = run_summary.get("provider_readiness")
        if not isinstance(readiness, dict):
            continue
        checked_records += int(readiness.get("checked_records") or 0)
        configured_records += int(readiness.get("configured_records") or 0)
        unconfigured_records += int(readiness.get("unconfigured_records") or 0)
        for name, count in dict(readiness.get("missing_env_counts") or {}).items():
            missing_env[str(name)] += int(count)
        for name, count in dict(readiness.get("provider_counts") or {}).items():
            providers[str(name)] += int(count)
    return {
        "checked_records": checked_records,
        "configured_records": configured_records,
        "unconfigured_records": unconfigured_records,
        "missing_env_counts": dict(missing_env),
        "provider_counts": dict(providers),
    }


def _summarize_realdb_suite_packet_schema_evidence(
    runs: list[dict[str, Any]],
) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key in ("full_packet", "provider_request"):
        checked_records = 0
        missing_records = 0
        missing_total = 0
        for run in runs:
            run_summary = run.get("summary")
            if not isinstance(run_summary, dict):
                continue
            packet_summary = run_summary.get("packet_schema_evidence")
            if not isinstance(packet_summary, dict):
                continue
            payload = packet_summary.get(key)
            if not isinstance(payload, dict):
                continue
            checked_records += int(payload.get("checked_records") or 0)
            missing_records += int(payload.get("missing_records") or 0)
            missing_total += int(payload.get("missing_total") or 0)
        summary[key] = {
            "checked_records": checked_records,
            "missing_records": missing_records,
            "missing_total": missing_total,
        }
    return summary


def _render_realdb_typed_fallback_mysql_suite_markdown(
    report: dict[str, Any],
) -> str:
    status = str(report.get("status", "unknown")).upper()
    summary = report["summary"]
    lines = [
        "# Real DB MySQL/MariaDB Typed Fallback Probe Suite",
        "",
        f"- status: `{status}`",
        f"- seeds: `{', '.join(str(seed) for seed in report.get('seeds', []))}`",
        f"- databases: `{', '.join(summary.get('databases', []))}`",
        f"- provider: `{report.get('provider')}`",
        f"- families: `{', '.join(report.get('families', []))}`",
        f"- safety mode: `{report.get('safety_mode')}`",
        "",
        "## Summary",
        "",
        f"- runs passed: `{summary['run_passed']}/{summary['run_total']}`",
        f"- runs skipped: `{summary['run_skipped']}`",
        f"- runs failed/error: `{summary['run_failed_or_error']}`",
        f"- questions: `{summary['questions']}`",
        f"- selected SQL: `{summary['selected']}/{summary['questions']}`",
        (
            "- typed fallback selected: "
            f"`{summary['typed_fallback_selected']}/{summary['questions']}`"
        ),
        f"- local selected: `{summary['local_selected']}`",
        f"- provider calls: `{summary['provider_call_count']}`",
        f"- provider errors: `{summary['provider_errors']}`",
        _render_provider_readiness_summary_line(
            summary.get("provider_readiness", {}),
        ),
        f"- render errors: `{summary['render_errors']}`",
        f"- execution ok: `{summary['execution_ok']}/{summary['questions']}`",
        (
            "- expected table/field matches: "
            f"`{summary['expected_field_matches']}/{summary['questions']}`"
        ),
        f"- expected kinds: `{summary.get('expected_kind_counts', {})}`",
        f"- result shape ok: `{summary.get('result_shape_ok', 0)}/{summary['questions']}`",
        f"- result shapes: `{summary.get('result_shape_counts', {})}`",
        f"- rows retained cases: `{summary['rows_retained_cases']}`",
        f"- sample-value rows: `{summary['sample_value_rows']}`",
        f"- packet schema evidence ok: `{summary.get('packet_schema_evidence_ok')}`",
        _render_packet_evidence_summary_line(
            "full rejected packet",
            summary.get("packet_schema_evidence", {}),
            "full_packet",
        ),
        _render_packet_evidence_summary_line(
            "compact provider request",
            summary.get("packet_schema_evidence", {}),
            "provider_request",
        ),
        "",
        "## Runs",
        "",
        (
            "| Seed | Database | Status | Questions | Selected | Exec OK | "
            "Expected | Provider Calls | Rows Retained | Artifact |"
        ),
        "|---:|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for run in report.get("runs", []):
        run_summary = run["summary"]
        artifact = str(run.get("out_dir") or "")
        lines.append(
            f"| {run.get('seed')} | `{run.get('database')}` | "
            f"`{str(run.get('status', '')).upper()}` | "
            f"`{run_summary.get('questions', 0)}` | "
            f"`{run_summary.get('selected', 0)}` | "
            f"`{run_summary.get('execution_ok', 0)}` | "
            f"`{run_summary.get('expected_field_matches', 0)}` | "
            f"`{run_summary.get('provider_call_count', 0)}` | "
            f"`{run_summary.get('rows_retained_cases', 0)}` | "
            f"`{artifact}` |"
        )
    lines.append("")
    return "\n".join(lines)


def _render_realdb_typed_fallback_postgres_suite_markdown(
    report: dict[str, Any],
) -> str:
    return _render_realdb_typed_fallback_mysql_suite_markdown(report).replace(
        "Real DB MySQL/MariaDB",
        "Real DB Postgres",
    )


@cli.command("realdb-typed-fallback-recover-report")
@click.option(
    "--report-json",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Existing realdb-typed-fallback-mysql-suite report JSON.",
)
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("target/realdb_typed_fallback_recovery"),
    show_default=True,
    help="Output directory for provider/proposal/render recovery artifacts.",
)
@click.option(
    "--provider",
    type=click.Choice(["none", *TYPED_PROVIDER_CHOICES]),
    default="none",
    show_default=True,
    help="Optional typed proposal provider for unresolved packet rows.",
)
@click.option(
    "--provider-base-url",
    type=str,
    default=None,
    help="Base URL for --provider openai-compatible. Appends /chat/completions unless already present.",
)
@click.option(
    "--provider-api-key-env",
    type=str,
    default=None,
    help="Environment variable containing the provider API key for --provider openai-compatible.",
)
@click.option(
    "--proposal-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Optional directory containing reviewed `*.proposal.json` files.",
)
@click.option(
    "--model",
    type=str,
    default=None,
    help="Provider model. Defaults are provider-specific.",
)
@click.option(
    "--db-url",
    type=str,
    default=None,
    help="Optional live MySQL/MariaDB server URL for read-only execution previews.",
)
@click.option(
    "--dialect",
    default="mysql",
    show_default=True,
    help="sqlglot dialect for local proposal rendering.",
)
@click.option("--max-cases", type=click.IntRange(min=1), default=None)
@click.option(
    "--exec-timeout-seconds",
    type=click.FloatRange(min=0.001),
    default=30.0,
    show_default=True,
)
@click.option("--out-json", type=click.Path(path_type=Path), default=None)
@click.option("--out-md", type=click.Path(path_type=Path), default=None)
@click.option(
    "--strict",
    is_flag=True,
    help="Exit non-zero unless every unresolved packet row recovers and validates.",
)
def realdb_typed_fallback_recover_report_cmd(
    report_json: Path,
    out_dir: Path,
    provider: str,
    provider_base_url: str | None,
    provider_api_key_env: str | None,
    proposal_dir: Path | None,
    model: str | None,
    db_url: str | None,
    dialect: str,
    max_cases: int | None,
    exec_timeout_seconds: float,
    out_json: Path | None,
    out_md: Path | None,
    strict: bool,
) -> None:
    """Recover unresolved real-DB packet rows through typed proposals only."""
    report = _run_realdb_typed_fallback_recover_report(
        report_json=report_json,
        out_dir=out_dir,
        provider=provider,
        provider_base_url=provider_base_url,
        provider_api_key_env=provider_api_key_env,
        proposal_dir=proposal_dir,
        model=model,
        db_url=db_url,
        dialect=dialect,
        max_cases=max_cases,
        exec_timeout_seconds=exec_timeout_seconds,
    )
    rendered = _render_realdb_typed_fallback_recovery_markdown(report)
    if out_json is not None:
        _write_json_report(out_json, report)
    if out_md is not None:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(rendered, encoding="utf-8")
    click.echo(rendered.rstrip())
    if strict and not report["summary"]["pass"]:
        raise click.ClickException("realdb typed fallback recovery did not pass")


def _run_realdb_typed_fallback_recover_report(
    *,
    report_json: Path,
    out_dir: Path,
    provider: str,
    provider_base_url: str | None,
    provider_api_key_env: str | None,
    proposal_dir: Path | None,
    model: str | None,
    db_url: str | None,
    dialect: str,
    max_cases: int | None,
    exec_timeout_seconds: float,
) -> dict[str, Any]:
    source_report = _read_json_file(report_json)
    rows = _realdb_unresolved_packet_rows(source_report)
    if max_cases is not None:
        rows = rows[:max_cases]
    out_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    for row in rows:
        run = row["run"]
        source_record = row["record"]
        packet_path = Path(str(row["packet"]))
        case_key = _realdb_recovery_case_key(run, packet_path)
        case_dir = out_dir / case_key
        selected_proposal = _realdb_recovery_proposal_path(
            proposal_dir,
            run=run,
            packet_path=packet_path,
        )
        database = str(run.get("database") or "")
        execute_db_url = (
            _mysql_url_with_database(db_url, database)
            if db_url and database
            else None
        )
        summary: dict[str, object]
        if selected_proposal is None and provider == "none":
            summary = {
                "selected_source": None,
                "selected_sql": None,
                "provider_call_count": 0,
                "provider_readiness": _typed_provider_readiness("none"),
                "provider_error": "no provider or reviewed proposal configured",
                "render_valid": None,
                "render_issue_count": None,
                "render_error": None,
                "execution": None,
                "used_direct_llm_sql": False,
            }
        else:
            summary = _run_llm_resolution_resolve_packet(
                packet_json=packet_path,
                proposal_json=selected_proposal,
                provider=None if selected_proposal is not None else provider,
                provider_base_url=provider_base_url,
                provider_api_key_env=provider_api_key_env,
                provider_out=case_dir / "provider.json",
                proposal_out=case_dir / "proposal.json",
                render_out=case_dir / "render.json",
                model=model,
                dialect=dialect,
                execute_db_url=execute_db_url,
                execution_out=case_dir / "execution.json",
                max_rows=1,
                retain_execution_rows=False,
                exec_timeout_seconds=exec_timeout_seconds,
            )
        selected_sql = str(summary.get("selected_sql") or "")
        execution = summary.get("execution")
        execution_status = (
            execution.get("status")
            if isinstance(execution, dict)
            else "not_requested"
        )
        rows_retained = (
            execution.get("rows_retained")
            if isinstance(execution, dict)
            else None
        )
        expected_match = _selected_sql_matches_typed_fallback_question(
            selected_sql,
            question=source_record,
        )
        records.append(
            {
                "case_key": case_key,
                "seed": run.get("seed"),
                "database": database,
                "index": source_record.get("index"),
                "question": source_record.get("question"),
                "expected_kind": source_record.get("expected_kind"),
                "expected_table": source_record.get("expected_table"),
                "expected_field": source_record.get("expected_field"),
                "expected_metric_table": source_record.get("expected_metric_table"),
                "expected_metric_field": source_record.get("expected_metric_field"),
                "expected_group_table": source_record.get("expected_group_table"),
                "expected_group_field": source_record.get("expected_group_field"),
                "expected_filter_table": source_record.get("expected_filter_table"),
                "expected_filter_field": source_record.get("expected_filter_field"),
                "expected_filter_value": source_record.get("expected_filter_value"),
                "expected_join_table": source_record.get("expected_join_table"),
                "expected_join_field": source_record.get("expected_join_field"),
                "expected_join_ref_table": source_record.get("expected_join_ref_table"),
                "expected_join_ref_field": source_record.get("expected_join_ref_field"),
                "expected_join_path": source_record.get("expected_join_path"),
                "packet": str(packet_path),
                "proposal": str(selected_proposal) if selected_proposal else None,
                "selected_source": summary.get("selected_source"),
                "selected_sql": selected_sql,
                "provider_call_count": summary.get("provider_call_count", 0),
                "provider_readiness": summary.get("provider_readiness"),
                "provider_error": summary.get("provider_error"),
                "render_valid": summary.get("render_valid"),
                "render_issue_count": summary.get("render_issue_count"),
                "render_error": summary.get("render_error"),
                "execution_status": execution_status,
                "rows_retained": rows_retained,
                "expected_match": expected_match,
                "used_direct_llm_sql": summary.get("used_direct_llm_sql"),
                "artifacts": {
                    "provider_result": str(case_dir / "provider.json")
                    if (case_dir / "provider.json").exists()
                    else None,
                    "proposal": str(case_dir / "proposal.json")
                    if (case_dir / "proposal.json").exists()
                    else None,
                    "render": str(case_dir / "render.json")
                    if (case_dir / "render.json").exists()
                    else None,
                    "execution": str(case_dir / "execution.json")
                    if (case_dir / "execution.json").exists()
                    else None,
                },
            }
        )
    summary = _summarize_realdb_typed_fallback_recovery_records(
        records,
        execution_requested=db_url is not None,
    )
    return {
        "schema_version": 1,
        "source": "realdb_typed_fallback_recover_report",
        "source_report": str(report_json),
        "out_dir": str(out_dir),
        "provider": provider,
        "model": model,
        "proposal_dir": str(proposal_dir) if proposal_dir else None,
        "database_url_redacted": redact_db_url(db_url) if db_url else None,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "safety_mode": (
            "reuses rejected-query packets; provider may propose typed plans "
            "only; SQL is locally rendered/validated"
            + (
                " and executed read-only with row values discarded"
                if db_url
                else "; execution not requested"
            )
        ),
        "summary": summary,
        "records": records,
    }


def _realdb_unresolved_packet_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    runs = report.get("runs")
    if not isinstance(runs, list):
        return rows
    for run in runs:
        if not isinstance(run, dict):
            continue
        records = run.get("records")
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            if str(record.get("selected_sql") or "").strip():
                continue
            artifacts = record.get("artifacts")
            if not isinstance(artifacts, dict):
                continue
            packet = artifacts.get("packet")
            if not packet:
                continue
            packet_path = Path(str(packet))
            if not packet_path.exists():
                continue
            rows.append({"run": run, "record": record, "packet": packet_path})
    return rows


def _realdb_recovery_case_key(run: dict[str, Any], packet_path: Path) -> str:
    seed = str(run.get("seed") or "seed")
    return f"seed-{seed}--{packet_path.parent.name}"


def _realdb_recovery_proposal_path(
    proposal_dir: Path | None,
    *,
    run: dict[str, Any],
    packet_path: Path,
) -> Path | None:
    if proposal_dir is None:
        return None
    case_key = _realdb_recovery_case_key(run, packet_path)
    candidates = [
        proposal_dir / f"{case_key}.proposal.json",
        proposal_dir / f"{packet_path.parent.name}.proposal.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _summarize_realdb_typed_fallback_recovery_records(
    records: list[dict[str, Any]],
    *,
    execution_requested: bool,
) -> dict[str, Any]:
    selected = [record for record in records if record.get("selected_sql")]
    expected_matches = sum(1 for record in records if record.get("expected_match"))
    execution_ok = sum(
        1 for record in records if record.get("execution_status") == "ok"
    )
    provider_errors = sum(1 for record in records if record.get("provider_error"))
    render_errors = sum(1 for record in records if record.get("render_valid") is False)
    direct_llm_sql = sum(1 for record in records if record.get("used_direct_llm_sql"))
    rows_retained = sum(1 for record in records if record.get("rows_retained") is True)
    pass_gate = (
        len(records) > 0
        and len(selected) == len(records)
        and expected_matches == len(records)
        and provider_errors == 0
        and render_errors == 0
        and direct_llm_sql == 0
        and (not execution_requested or execution_ok == len(records))
        and (not execution_requested or rows_retained == 0)
    )
    return {
        "unresolved_input_count": len(records),
        "selected": len(selected),
        "selected_sources": dict(
            Counter(str(record.get("selected_source") or "none") for record in records)
        ),
        "provider_call_count": sum(
            int(record.get("provider_call_count") or 0) for record in records
        ),
        "provider_errors": provider_errors,
        "render_errors": render_errors,
        "expected_matches": expected_matches,
        "execution_requested": execution_requested,
        "execution_ok": execution_ok,
        "rows_retained_cases": rows_retained,
        "direct_llm_sql_count": direct_llm_sql,
        "pass": pass_gate,
    }


def _render_realdb_typed_fallback_recovery_markdown(
    report: dict[str, Any],
) -> str:
    summary = report["summary"]
    lines = [
        "# Real DB Typed Fallback Recovery",
        "",
        f"- source report: `{report.get('source_report')}`",
        f"- provider: `{report.get('provider')}`",
        f"- model: `{report.get('model')}`",
        f"- safety mode: `{report.get('safety_mode')}`",
        "",
        "## Summary",
        "",
        f"- unresolved input rows: `{summary['unresolved_input_count']}`",
        f"- selected SQL: `{summary['selected']}/{summary['unresolved_input_count']}`",
        f"- selected sources: `{summary['selected_sources']}`",
        f"- provider calls: `{summary['provider_call_count']}`",
        f"- provider errors: `{summary['provider_errors']}`",
        f"- render errors: `{summary['render_errors']}`",
        f"- expected matches: `{summary['expected_matches']}/{summary['unresolved_input_count']}`",
        f"- execution requested: `{summary['execution_requested']}`",
        f"- execution ok: `{summary['execution_ok']}/{summary['unresolved_input_count']}`",
        f"- rows retained cases: `{summary['rows_retained_cases']}`",
        f"- direct LLM SQL count: `{summary['direct_llm_sql_count']}`",
        f"- pass: `{summary['pass']}`",
        "",
        "## Records",
        "",
        (
            "| Case | Database | Question | Source | Provider Calls | Render | "
            "Exec | Expected | Error |"
        ),
        "|---|---|---|---|---:|---|---|---|---|",
    ]
    for record in report.get("records", []):
        error = record.get("provider_error") or record.get("render_error") or ""
        lines.append(
            f"| `{record.get('case_key')}` | `{record.get('database')}` | "
            f"`{record.get('question')}` | `{record.get('selected_source')}` | "
            f"`{record.get('provider_call_count', 0)}` | "
            f"`{record.get('render_valid')}` | "
            f"`{record.get('execution_status')}` | "
            f"`{record.get('expected_match')}` | "
            f"`{error}` |"
        )
    lines.append("")
    return "\n".join(lines)


def _run_realdb_typed_fallback_mysql(
    *,
    out_dir: Path,
    seed: int,
    db_url: str | None,
    database: str | None,
    probe_count: int,
    families: tuple[str, ...],
    provider: str,
    provider_base_url: str | None,
    provider_api_key_env: str | None,
    model: str | None,
    semsql_bin: Path,
    graph_cache_dir: Path | None,
    timeout_seconds: int,
    extract_timeout_seconds: int,
    exec_timeout_seconds: float,
    include_sample_values: bool,
    include_generated: bool,
    engine: str = "mysql",
) -> dict[str, Any]:
    if engine not in {"mysql", "postgres"}:
        raise ValueError(f"unsupported realdb typed fallback engine: {engine}")
    resolved_url = db_url or os.environ.get(
        "SEMSQL_POSTGRES_PROBE_URL" if engine == "postgres" else "SEMSQL_MYSQL_PROBE_URL"
    )
    if not resolved_url:
        return _realdb_typed_fallback_skipped_report(
            out_dir=out_dir,
            seed=seed,
            reason="missing_db_url",
            detail=(
                "pass --db-url or set SEMSQL_POSTGRES_PROBE_URL"
                if engine == "postgres"
                else "pass --db-url or set SEMSQL_MYSQL_PROBE_URL"
            ),
            engine=engine,
        )
    driver: object
    if engine == "postgres":
        try:
            driver = _import_postgres_driver()
        except ImportError:
            return _realdb_typed_fallback_skipped_report(
                out_dir=out_dir,
                seed=seed,
                reason="missing_postgres_driver",
                detail="run with `uv run --extra db ...` or install psycopg/psycopg2",
                engine=engine,
            )
    else:
        try:
            driver = import_module("pymysql")
        except ImportError:
            return _realdb_typed_fallback_skipped_report(
                out_dir=out_dir,
                seed=seed,
                reason="missing_pymysql",
                detail="run with `uv run --extra db ...` or install pymysql",
                engine=engine,
            )

    out_dir.mkdir(parents=True, exist_ok=True)
    selected_database = database or _database_from_url(resolved_url)
    try:
        conn = (
            _postgres_connect(
                cast(tuple[str, Any], driver),
                resolved_url,
                database=selected_database,
            )
            if engine == "postgres"
            else _mysql_connect(
                driver,
                resolved_url,
                database=None,
                autocommit=False,
            )
        )
        try:
            if selected_database is None and engine == "postgres":
                selected_database = _postgres_current_database(conn)
            elif selected_database is None:
                selected_database = _select_random_database(
                    conn,
                    seed=seed,
                    include_generated=include_generated,
                )
            if (
                engine == "postgres"
                and not include_generated
                and selected_database.lower().startswith(("semsql_", "test_"))
            ):
                raise RuntimeError(
                    "selected Postgres database looks generated; pass --include-generated to probe it"
                )
            if engine == "postgres":
                tables = _list_postgres_tables(conn, selected_database)
                columns = _list_postgres_columns(conn, selected_database)
                relationships = _list_postgres_relationships(conn, selected_database)
            else:
                tables = _list_tables(conn, selected_database)
                columns = _list_columns(conn, selected_database)
                relationships = _list_relationships(conn, selected_database)
        finally:
            conn.close()
    except Exception as error:
        return _realdb_typed_fallback_error_report(
            out_dir=out_dir,
            seed=seed,
            database=selected_database,
            db_url=resolved_url,
            error=error,
            engine=engine,
        )

    safe_tables = [table for table in tables if SAFE_IDENTIFIER_RE.match(table.table)]
    skipped_tables = [table.table for table in tables if table not in safe_tables]
    ambiguous_physical_tables = _ambiguous_physical_family_tables(safe_tables)
    routable_tables = [
        table
        for table in safe_tables
        if table.table.lower() not in ambiguous_physical_tables
    ]
    routable_columns = [
        column
        for column in columns
        if column.table.lower() not in ambiguous_physical_tables
    ]
    routable_relationships = [
        relationship
        for relationship in relationships
        if relationship.table.lower() not in ambiguous_physical_tables
        and relationship.referenced_table.lower() not in ambiguous_physical_tables
    ]
    database_url = (
        _postgres_url_with_database(resolved_url, selected_database)
        if engine == "postgres"
        else _mysql_url_with_database(resolved_url, selected_database)
    )
    graph_root = graph_cache_dir or (out_dir / "graphs")
    graph_path = graph_root / f"{selected_database}.schemaonly.semsql"
    try:
        build_graph_for_db_url(
            semsql_bin,
            database_url,
            graph_path,
            path_arg=out_dir,
            timeout_seconds=extract_timeout_seconds,
            sample_values=include_sample_values,
        )
    except Exception as error:
        return _realdb_typed_fallback_error_report(
            out_dir=out_dir,
            seed=seed,
            database=selected_database,
            db_url=database_url,
            error=error,
            engine=engine,
        )
    sample_value_rows = _graph_sample_value_count(graph_path)
    sample_values = _graph_sample_values(graph_path) if include_sample_values else {}
    questions = _select_realdb_typed_fallback_questions(
        routable_tables,
        routable_columns,
        routable_relationships,
        sample_values=sample_values,
        seed=seed,
        probe_count=probe_count,
        families=families,
    )

    records: list[dict[str, Any]] = []
    for index, question in enumerate(questions, start=1):
        case_dir = out_dir / _realdb_fallback_case_slug(index, str(question["question"]))
        summary = _run_llm_resolution_fallback_query(
            graph=graph_path,
            question=str(question["question"]),
            out_dir=case_dir,
            semsql_bin=semsql_bin,
            cascade_manifest=None,
            intent_yaml=None,
            dialect=engine,
            execute_sqlite=None,
            execute_db_url=database_url,
            max_rows=1,
            retain_execution_rows=False,
            exec_timeout_seconds=exec_timeout_seconds,
            timeout_seconds=timeout_seconds,
            include_samples=include_sample_values,
            provider=provider,
            provider_base_url=provider_base_url,
            provider_api_key_env=provider_api_key_env,
            proposal_json=None,
            model=model,
        )
        execution = summary.get("execution")
        execution_status = (
            execution.get("status")
            if isinstance(execution, dict)
            else "not_requested"
        )
        rows_retained = (
            execution.get("rows_retained")
            if isinstance(execution, dict)
            else None
        )
        selected_sql = str(summary.get("selected_sql") or "")
        expected_table = str(question["expected_table"])
        expected_field = str(question["expected_field"])
        expected_kind = str(question["expected_kind"])
        expected_match = _selected_sql_matches_typed_fallback_question(
            selected_sql,
            question=question,
        )
        packet_schema_evidence = _realdb_typed_fallback_packet_schema_evidence(
            question=question,
            artifacts=summary.get("artifacts"),
        )
        result_shape = summary.get("result_shape")
        result_shape_kind = _typed_fallback_result_shape_kind(result_shape)
        result_shape_ok = _typed_fallback_result_shape_ok(
            question,
            result_shape,
        )
        ok = (
            bool(selected_sql)
            and summary.get("used_direct_llm_sql") is False
            and expected_match
            and result_shape_ok
            and execution_status == "ok"
            and rows_retained is False
            and not summary.get("provider_error")
            and summary.get("fallback_render_valid") is not False
        )
        records.append(
            {
                "index": index,
                "question": question["question"],
                "expected_kind": expected_kind,
                "expected_table": expected_table,
                "expected_field": expected_field,
                "expected_metric_table": question.get("expected_metric_table"),
                "expected_metric_field": question.get("expected_metric_field"),
                "expected_group_table": question.get("expected_group_table"),
                "expected_group_field": question.get("expected_group_field"),
                "expected_time_table": question.get("expected_time_table"),
                "expected_time_field": question.get("expected_time_field"),
                "expected_filter_table": question.get("expected_filter_table"),
                "expected_filter_field": question.get("expected_filter_field"),
                "expected_filter_value": question.get("expected_filter_value"),
                "expected_join_table": question.get("expected_join_table"),
                "expected_join_field": question.get("expected_join_field"),
                "expected_join_ref_table": question.get("expected_join_ref_table"),
                "expected_join_ref_field": question.get("expected_join_ref_field"),
                "expected_join_path": question.get("expected_join_path"),
                "selected_source": summary.get("selected_source"),
                "local_routed": summary.get("local_routed"),
                "local_stage_pinned": summary.get("local_stage_pinned"),
                "provider_call_count": summary.get("provider_call_count", 0),
                "provider_readiness": summary.get("provider_readiness"),
                "fallback_render_valid": summary.get("fallback_render_valid"),
                "fallback_render_issue_count": summary.get(
                    "fallback_render_issue_count"
                ),
                "execution_status": execution_status,
                "rows_retained": rows_retained,
                "expected_match": expected_match,
                "result_shape_kind": result_shape_kind,
                "result_shape_ok": result_shape_ok,
                "result_shape": result_shape,
                "packet_schema_evidence": packet_schema_evidence,
                "ok": ok,
                "selected_sql": selected_sql,
                "artifacts": summary.get("artifacts"),
                "provider_error": summary.get("provider_error"),
                "fallback_render_error": summary.get("fallback_render_error"),
            }
        )

    summary = _summarize_realdb_typed_fallback_records(
        records,
        provider=provider,
        sample_value_rows=sample_value_rows,
        sample_values_allowed=include_sample_values,
    )
    high_risk = name_looks_sensitive(selected_database) or any(
        table.sensitive for table in safe_tables
    )
    status = "pass" if summary["pass"] else "fail"
    if not questions:
        status = "skipped"
    return {
        "schema_version": 1,
        "engine": engine,
        "status": status,
        "seed": seed,
        "database": selected_database,
        "database_url_redacted": redact_db_url(database_url),
        "out_dir": str(out_dir),
        "graph": str(graph_path),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "safety_mode": (
            "schema-only extraction; "
            f"{'bounded non-redacted sample values included' if include_sample_values else 'no sample values'}; "
            "provider may propose "
            "typed plans only; SQL is locally rendered/validated and executed "
            "read-only with row values discarded"
        ),
        "provider": provider,
        "model": model,
        "families": list(families),
        "high_risk_schema": high_risk,
        "sensitive_tables": [table.table for table in safe_tables if table.sensitive],
        "skipped_unsafe_identifier_tables": skipped_tables,
        "ambiguous_physical_family_tables": sorted(ambiguous_physical_tables),
        "summary": summary,
        "records": records,
    }


def _select_realdb_typed_fallback_questions(
    tables: list[Any],
    columns: list[Any],
    relationships: list[Any],
    *,
    sample_values: dict[str, list[str]] | None = None,
    seed: int,
    probe_count: int,
    families: tuple[str, ...],
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    family_set = tuple(dict.fromkeys(families or ("rate",)))
    for family_index, family in enumerate(family_set):
        family_seed = seed + family_index * 1009
        if family == "rate":
            selected.extend(
                select_typed_fallback_rate_questions(
                    tables,
                    columns,
                    seed=family_seed,
                    probe_count=probe_count,
                )
            )
        elif family == "grouped_avg":
            selected.extend(
                select_typed_fallback_grouped_metric_questions(
                    tables,
                    columns,
                    seed=family_seed,
                    probe_count=probe_count,
                )
            )
        elif family == "multi_series_grouped_avg":
            selected.extend(
                select_typed_fallback_multi_series_metric_questions(
                    tables,
                    columns,
                    seed=family_seed,
                    probe_count=probe_count,
                )
            )
        elif family == "filtered_grouped_avg":
            selected.extend(
                select_typed_fallback_filtered_grouped_metric_questions(
                    tables,
                    columns,
                    seed=family_seed,
                    probe_count=probe_count,
                )
            )
        elif family == "value_filtered_grouped_avg":
            selected.extend(
                select_typed_fallback_value_filtered_grouped_metric_questions(
                    tables,
                    columns,
                    sample_values=sample_values or {},
                    seed=family_seed,
                    probe_count=probe_count,
                )
            )
        elif family == "joined_filtered_grouped_avg":
            selected.extend(
                select_typed_fallback_joined_filtered_grouped_metric_questions(
                    tables,
                    columns,
                    relationships,
                    seed=family_seed,
                    probe_count=probe_count,
                )
            )
        elif family == "multi_joined_filtered_grouped_avg":
            selected.extend(
                select_typed_fallback_multi_joined_filtered_grouped_metric_questions(
                    tables,
                    columns,
                    relationships,
                    seed=family_seed,
                    probe_count=probe_count,
                )
            )
    return selected


def _summarize_realdb_typed_fallback_records(
    records: list[dict[str, Any]],
    *,
    provider: str,
    sample_value_rows: int,
    sample_values_allowed: bool,
) -> dict[str, Any]:
    selected = [row for row in records if row.get("selected_sql")]
    typed = [row for row in records if row.get("selected_source") == "typed_fallback"]
    provider_calls = sum(int(row.get("provider_call_count") or 0) for row in records)
    execution_ok = sum(1 for row in records if row.get("execution_status") == "ok")
    expected_match = sum(1 for row in records if row.get("expected_match"))
    kinds = Counter(str(row.get("expected_kind") or "unknown") for row in records)
    result_shape_counts = Counter(
        str(
            row.get("result_shape_kind")
            or _typed_fallback_result_shape_kind(row.get("result_shape"))
        )
        for row in records
    )
    result_shape_ok = sum(1 for row in records if row.get("result_shape_ok") is True)
    ok_count = sum(1 for row in records if row.get("ok"))
    provider_errors = sum(1 for row in records if row.get("provider_error"))
    render_errors = sum(1 for row in records if row.get("fallback_render_valid") is False)
    rows_retained = sum(1 for row in records if row.get("rows_retained") is not False)
    packet_evidence = _summarize_realdb_packet_schema_evidence(records)
    packet_evidence_ok = all(
        int(payload.get("missing_total") or 0) == 0
        for payload in packet_evidence.values()
        if isinstance(payload, dict)
    )
    provider_readiness = _summarize_realdb_provider_readiness(records)
    provider_expectation_ok = provider == "none" or provider_calls > 0 or len(selected) == len(records)
    return {
        "questions": len(records),
        "selected": len(selected),
        "typed_fallback_selected": len(typed),
        "local_selected": sum(1 for row in records if row.get("selected_source") == "local"),
        "provider_call_count": provider_calls,
        "provider_errors": provider_errors,
        "render_errors": render_errors,
        "execution_ok": execution_ok,
        "expected_field_matches": expected_match,
        "expected_kind_counts": dict(kinds),
        "result_shape_ok": result_shape_ok,
        "result_shape_counts": dict(result_shape_counts),
        "rows_retained_cases": rows_retained,
        "ok": ok_count,
        "sample_value_rows": sample_value_rows,
        "packet_schema_evidence": packet_evidence,
        "packet_schema_evidence_ok": packet_evidence_ok,
        "provider_readiness": provider_readiness,
        "pass": (
            len(records) > 0
            and ok_count == len(records)
            and len(selected) == len(records)
            and execution_ok == len(records)
            and expected_match == len(records)
            and result_shape_ok == len(records)
            and (sample_values_allowed or sample_value_rows == 0)
            and provider_errors == 0
            and render_errors == 0
            and rows_retained == 0
            and provider_expectation_ok
            and packet_evidence_ok
        ),
        "skipped": len(records) == 0,
    }


def _typed_fallback_result_shape_kind(result_shape: object) -> str:
    if not isinstance(result_shape, dict):
        return "missing"
    kind = str(result_shape.get("kind") or "")
    return kind or "missing"


def _typed_fallback_result_shape_ok(
    question: dict[str, Any],
    result_shape: object,
) -> bool:
    kind = _typed_fallback_result_shape_kind(result_shape)
    if kind in {"missing", "unknown"}:
        return False
    expected_kind = str(question.get("expected_kind") or "")
    if expected_kind == "conditional_rate":
        return kind == "scalar_metric"
    if expected_kind == "multi_series_grouped_avg":
        return kind == "multi_series_chart"
    if expected_kind in {
        "grouped_avg",
        "filtered_grouped_avg",
        "value_filtered_grouped_avg",
        "joined_filtered_grouped_avg",
        "multi_joined_filtered_grouped_avg",
    }:
        return kind in {"categorical_chart", "time_series_chart", "multi_series_chart"}
    return False


def _render_realdb_typed_fallback_mysql_markdown(report: dict[str, Any]) -> str:
    status = str(report.get("status", "unknown")).upper()
    summary = report["summary"]
    lines = [
        "# Real DB MySQL/MariaDB Typed Fallback Probe",
        "",
        f"- status: `{status}`",
        f"- database: `{report.get('database')}`",
        f"- graph: `{report.get('graph')}`",
        f"- provider: `{report.get('provider')}`",
        f"- families: `{', '.join(report.get('families', []))}`",
        f"- high-risk schema: `{report.get('high_risk_schema')}`",
        f"- safety mode: `{report.get('safety_mode')}`",
        "",
        "## Summary",
        "",
        f"- questions: `{summary['questions']}`",
        f"- selected SQL: `{summary['selected']}/{summary['questions']}`",
        (
            "- typed fallback selected: "
            f"`{summary['typed_fallback_selected']}/{summary['questions']}`"
        ),
        f"- local selected: `{summary['local_selected']}`",
        f"- provider calls: `{summary['provider_call_count']}`",
        f"- provider errors: `{summary['provider_errors']}`",
        _render_provider_readiness_summary_line(
            summary.get("provider_readiness", {}),
        ),
        f"- render errors: `{summary['render_errors']}`",
        f"- execution ok: `{summary['execution_ok']}/{summary['questions']}`",
        (
            "- expected table/field matches: "
            f"`{summary['expected_field_matches']}/{summary['questions']}`"
        ),
        f"- expected kinds: `{summary.get('expected_kind_counts', {})}`",
        f"- result shape ok: `{summary.get('result_shape_ok', 0)}/{summary['questions']}`",
        f"- result shapes: `{summary.get('result_shape_counts', {})}`",
        f"- rows retained cases: `{summary['rows_retained_cases']}`",
        f"- sample-value rows: `{summary['sample_value_rows']}`",
        f"- packet schema evidence ok: `{summary.get('packet_schema_evidence_ok')}`",
        _render_packet_evidence_summary_line(
            "full rejected packet",
            summary.get("packet_schema_evidence", {}),
            "full_packet",
        ),
        _render_packet_evidence_summary_line(
            "compact provider request",
            summary.get("packet_schema_evidence", {}),
            "provider_request",
        ),
        "",
        "## Records",
        "",
        (
            "| # | Question | Expected | Source | Provider Calls | Render | "
            "Exec | Rows Retained | Expected Match | Shape | OK | SQL |"
        ),
        "|---:|---|---|---|---:|---|---|---|---|---|---|---|",
    ]
    for row in report.get("records", []):
        expected = _realdb_fallback_expected_ref(row)
        sql_html = html_escape(str(row.get("selected_sql") or ""))
        lines.append(
            f"| {row['index']} | `{row['question']}` | `{expected}` | "
            f"`{row.get('selected_source')}` | "
            f"`{row.get('provider_call_count', 0)}` | "
            f"`{row.get('fallback_render_valid')}` | "
            f"`{row.get('execution_status')}` | "
            f"`{row.get('rows_retained')}` | "
            f"`{row.get('expected_match')}` | "
            f"`{row.get('result_shape_kind')}` | "
            f"`{row.get('ok')}` | "
            f"<code>{sql_html}</code> |"
        )
    lines.append("")
    if report.get("status") == "skipped":
        lines.extend(
            [
                "## Skip Reason",
                "",
                f"- reason: `{report.get('skip_reason')}`",
                f"- detail: {report.get('skip_detail')}",
                "",
            ]
        )
    if report.get("status") == "error":
        lines.extend(["## Error", "", f"- detail: {report.get('error_detail')}", ""])
    _append_packet_evidence_issues(lines, report.get("records", []))
    return "\n".join(lines)


def _render_realdb_typed_fallback_postgres_markdown(report: dict[str, Any]) -> str:
    return _render_realdb_typed_fallback_mysql_markdown(report).replace(
        "Real DB MySQL/MariaDB",
        "Real DB Postgres",
    )


def _render_packet_evidence_summary_line(
    label: str,
    evidence_summary: object,
    key: str,
) -> str:
    if not isinstance(evidence_summary, dict):
        return f"- {label} schema evidence: `0 checked, 0 missing records, 0 missing facts`"
    payload = evidence_summary.get(key)
    if not isinstance(payload, dict):
        return f"- {label} schema evidence: `0 checked, 0 missing records, 0 missing facts`"
    return (
        f"- {label} schema evidence: "
        f"`{int(payload.get('checked_records') or 0)} checked, "
        f"{int(payload.get('missing_records') or 0)} missing records, "
        f"{int(payload.get('missing_total') or 0)} missing facts`"
    )


def _render_provider_readiness_summary_line(readiness: object) -> str:
    if not isinstance(readiness, dict):
        return "- provider readiness: `not recorded`"
    checked = int(readiness.get("checked_records") or 0)
    configured = int(readiness.get("configured_records") or 0)
    unconfigured = int(readiness.get("unconfigured_records") or 0)
    missing_env = readiness.get("missing_env_counts") or {}
    providers = readiness.get("provider_counts") or {}
    return (
        "- provider readiness: "
        f"`{configured}/{checked} configured, {unconfigured} unconfigured; "
        f"providers={dict(providers)}, missing_env={dict(missing_env)}`"
    )


def _append_packet_evidence_issues(lines: list[str], records: object) -> None:
    issue_lines: list[str] = []
    if isinstance(records, list):
        for row in records:
            if not isinstance(row, dict):
                continue
            evidence = row.get("packet_schema_evidence")
            if not isinstance(evidence, dict):
                continue
            for key, label in (
                ("full_packet", "full packet"),
                ("provider_request", "provider request"),
            ):
                payload = evidence.get(key)
                if not isinstance(payload, dict):
                    continue
                missing = payload.get("missing")
                if not isinstance(missing, list) or not missing:
                    continue
                issue_lines.append(
                    f"- case `{row.get('index')}` {label}: "
                    f"`{', '.join(str(item) for item in missing)}`"
                )
    if issue_lines:
        lines.extend(["", "## Packet Evidence Issues", "", *issue_lines, ""])


def _summarize_realdb_packet_schema_evidence(
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for key in ("full_packet", "provider_request"):
        checked_records = 0
        missing_records = 0
        missing_total = 0
        for row in records:
            evidence = row.get("packet_schema_evidence")
            if not isinstance(evidence, dict):
                continue
            payload = evidence.get(key)
            if not isinstance(payload, dict) or payload.get("checked") is not True:
                continue
            checked_records += 1
            row_missing = int(payload.get("missing_count") or 0)
            if row_missing:
                missing_records += 1
                missing_total += row_missing
        summary[key] = {
            "checked_records": checked_records,
            "missing_records": missing_records,
            "missing_total": missing_total,
        }
    return summary


def _summarize_realdb_provider_readiness(
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    checked_records = 0
    configured_records = 0
    unconfigured_records = 0
    missing_env: Counter[str] = Counter()
    providers: Counter[str] = Counter()
    for row in records:
        readiness = row.get("provider_readiness")
        if not isinstance(readiness, dict):
            continue
        checked_records += 1
        provider_name = readiness.get("provider")
        if provider_name not in {None, ""}:
            providers[str(provider_name)] += 1
        if readiness.get("configured") is True:
            configured_records += 1
        else:
            unconfigured_records += 1
        missing = readiness.get("missing_env")
        if isinstance(missing, list):
            for name in missing:
                missing_env[str(name)] += 1
    return {
        "checked_records": checked_records,
        "configured_records": configured_records,
        "unconfigured_records": unconfigured_records,
        "missing_env_counts": dict(missing_env),
        "provider_counts": dict(providers),
    }


def _realdb_typed_fallback_packet_schema_evidence(
    *,
    question: dict[str, Any],
    artifacts: object,
) -> dict[str, Any]:
    expected_refs = _realdb_typed_fallback_expected_schema_refs(question)
    evidence: dict[str, Any] = {
        "expected_ref_count": len(expected_refs),
        "full_packet": _schema_packet_evidence_from_path(
            _artifact_path(artifacts, "packet"),
            expected_refs=expected_refs,
            provider_request=False,
        ),
        "provider_request": _schema_packet_evidence_from_path(
            _artifact_path(artifacts, "openai_request"),
            expected_refs=expected_refs,
            provider_request=True,
        ),
    }
    return evidence


def _artifact_path(artifacts: object, key: str) -> Path | None:
    if not isinstance(artifacts, dict):
        return None
    value = artifacts.get(key)
    if not isinstance(value, str) or not value:
        return None
    return Path(value)


def _schema_packet_evidence_from_path(
    path: Path | None,
    *,
    expected_refs: set[str],
    provider_request: bool,
) -> dict[str, Any]:
    if path is None:
        return {"checked": False, "missing_count": 0, "missing": []}
    if not path.exists():
        return {
            "checked": False,
            "missing_count": 0,
            "missing": [],
            "error": f"missing artifact: {path}",
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        packet = (
            _packet_from_openai_request_preview(payload)
            if provider_request
            else payload.get("packet", payload)
        )
    except (OSError, json.JSONDecodeError, TypeError) as exc:
        return {
            "checked": False,
            "missing_count": 0,
            "missing": [],
            "error": str(exc),
        }
    if not isinstance(packet, dict):
        return {
            "checked": False,
            "missing_count": 0,
            "missing": [],
            "error": "artifact did not contain a packet object",
        }
    missing = sorted(expected_refs - _schema_card_available_refs(packet))
    return {
        "checked": True,
        "missing_count": len(missing),
        "missing": missing,
    }


def _packet_from_openai_request_preview(payload: dict[str, Any]) -> object:
    input_payload = payload.get("input")
    if isinstance(input_payload, str):
        return json.loads(input_payload)
    return payload.get("packet", payload)


def _schema_card_available_refs(packet: dict[str, Any]) -> set[str]:
    schema_card = packet.get("schema_card")
    if not isinstance(schema_card, dict):
        return set()
    refs: set[str] = set()
    entities = schema_card.get("entities")
    if not isinstance(entities, list):
        return refs
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        entity_name = str(entity.get("name") or "")
        if not entity_name:
            continue
        refs.add(f"entity:{entity_name}")
        fields = entity.get("fields")
        if not isinstance(fields, list):
            continue
        for field in fields:
            if not isinstance(field, dict):
                continue
            field_name = str(field.get("name") or field.get("field") or "")
            if field_name:
                refs.add(f"field:{entity_name}.{field_name}")
    return refs


def _realdb_typed_fallback_expected_schema_refs(
    question: dict[str, Any],
) -> set[str]:
    refs: set[str] = set()
    for key in (
        "expected_table",
        "expected_metric_table",
        "expected_group_table",
        "expected_time_table",
        "expected_filter_table",
        "expected_join_table",
        "expected_join_ref_table",
    ):
        table = question.get(key)
        if isinstance(table, str) and table:
            refs.add(f"entity:{table}")

    def add_field(table_key: str, field_key: str, *, fallback_table: str | None = None) -> None:
        table = question.get(table_key)
        if not isinstance(table, str) or not table:
            table = fallback_table
        field = question.get(field_key)
        if isinstance(table, str) and table and isinstance(field, str) and field:
            refs.add(f"field:{table}.{field}")

    base_table = question.get("expected_table")
    fallback_table = base_table if isinstance(base_table, str) else None
    add_field("expected_table", "expected_field")
    add_field("expected_metric_table", "expected_metric_field", fallback_table=fallback_table)
    add_field("expected_group_table", "expected_group_field", fallback_table=fallback_table)
    add_field("expected_time_table", "expected_time_field", fallback_table=fallback_table)
    add_field("expected_filter_table", "expected_filter_field", fallback_table=fallback_table)
    add_field("expected_join_table", "expected_join_field")
    add_field("expected_join_ref_table", "expected_join_ref_field")

    join_path = question.get("expected_join_path")
    if isinstance(join_path, list):
        for step in join_path:
            if not isinstance(step, dict):
                continue
            for table_key, field_key in (
                ("left_table", "left_field"),
                ("right_table", "right_field"),
                ("from_table", "from_field"),
                ("to_table", "to_field"),
                ("from_entity", "from_field"),
                ("to_entity", "to_field"),
            ):
                table = step.get(table_key)
                field = step.get(field_key)
                if isinstance(table, str) and table and isinstance(field, str) and field:
                    refs.add(f"entity:{table}")
                    refs.add(f"field:{table}.{field}")
    return refs


def _realdb_fallback_expected_ref(row: dict[str, Any]) -> str:
    kind = str(row.get("expected_kind") or "")
    table = str(row.get("expected_table") or "")
    if kind in {"joined_filtered_grouped_avg", "multi_joined_filtered_grouped_avg"}:
        suffix = ""
        if row.get("expected_filter_field"):
            suffix = (
                f" where {row.get('expected_filter_table')}.{row.get('expected_filter_field')} "
                f"= {row.get('expected_filter_value')}"
            )
        join_path = row.get("expected_join_path")
        if isinstance(join_path, list) and join_path:
            path_ref = " -> ".join(
                f"{step.get('left_table')}.{step.get('left_field')}="
                f"{step.get('right_table')}.{step.get('right_field')}"
                for step in join_path
                if isinstance(step, dict)
            )
        else:
            path_ref = (
                f"{row.get('expected_join_table')}.{row.get('expected_join_field')}"
                f" = {row.get('expected_join_ref_table')}.{row.get('expected_join_ref_field')}"
            )
        return (
            f"{row.get('expected_metric_table')}.{row.get('expected_metric_field')} "
            f"by {row.get('expected_group_table')}.{row.get('expected_group_field')}"
            f"{suffix} via {path_ref}"
        )
    if kind == "multi_series_grouped_avg":
        return (
            f"{table}.{row.get('expected_metric_field')} by "
            f"{table}.{row.get('expected_group_field')} over "
            f"{table}.{row.get('expected_time_field')}"
        )
    if kind in {"grouped_avg", "filtered_grouped_avg", "value_filtered_grouped_avg"}:
        suffix = ""
        if kind in {"filtered_grouped_avg", "value_filtered_grouped_avg"}:
            suffix = (
                f" where {table}.{row.get('expected_filter_field')} "
                f"= {row.get('expected_filter_value')}"
            )
        return (
            f"{table}.{row.get('expected_metric_field')} "
            f"by {table}.{row.get('expected_group_field')}{suffix}"
        )
    return f"{table}.{row.get('expected_field')}"


def _selected_sql_matches_typed_fallback_question(
    sql: str,
    *,
    question: dict[str, Any],
) -> bool:
    kind = str(question.get("expected_kind") or "")
    table = str(question.get("expected_table") or "")
    if kind == "conditional_rate":
        return _conditional_rate_sql_mentions_expected_field(
            sql,
            table=table,
            field=str(question.get("expected_field") or ""),
        )
    if kind == "multi_series_grouped_avg":
        return _multi_series_grouped_avg_sql_mentions_expected_fields(
            sql,
            table=table,
            metric_field=str(question.get("expected_metric_field") or ""),
            time_field=str(question.get("expected_time_field") or ""),
            group_field=str(question.get("expected_group_field") or ""),
        )
    if kind in {"grouped_avg", "filtered_grouped_avg", "value_filtered_grouped_avg"}:
        grouped_match = _grouped_avg_sql_mentions_expected_fields(
            sql,
            table=table,
            metric_field=str(question.get("expected_metric_field") or ""),
            group_field=str(question.get("expected_group_field") or ""),
        )
        if not grouped_match or kind == "grouped_avg":
            return grouped_match
        return _sql_mentions_expected_filter(
            sql,
            table=table,
            field=str(question.get("expected_filter_field") or ""),
            value=str(question.get("expected_filter_value") or ""),
        )
    if kind in {"joined_filtered_grouped_avg", "multi_joined_filtered_grouped_avg"}:
        grouped_match = _grouped_avg_sql_mentions_expected_fields(
            sql,
            table=str(question.get("expected_metric_table") or ""),
            metric_field=str(question.get("expected_metric_field") or ""),
            group_field=str(question.get("expected_group_field") or ""),
            group_table=str(question.get("expected_group_table") or ""),
        )
        if not grouped_match:
            return False
        if not _sql_mentions_expected_join_path(sql, question=question):
            return False
        filter_field = str(question.get("expected_filter_field") or "")
        if not filter_field:
            return True
        return _sql_mentions_expected_filter(
            sql,
            table=str(question.get("expected_filter_table") or ""),
            field=filter_field,
            value=str(question.get("expected_filter_value") or ""),
        )
    return False


def _conditional_rate_sql_mentions_expected_field(
    sql: str,
    *,
    table: str,
    field: str,
) -> bool:
    normalized = sql.replace("`", "").replace('"', "").lower()
    compact = re.sub(r"\s+", " ", normalized)
    expected_ref = f"{table.lower()}.{field.lower()}"
    return (
        "sum(case when" in compact
        and "count(" in compact
        and "nullif(" in compact
        and expected_ref in compact
    )


def _multi_series_grouped_avg_sql_mentions_expected_fields(
    sql: str,
    *,
    table: str,
    metric_field: str,
    time_field: str,
    group_field: str,
) -> bool:
    normalized = sql.replace("`", "").replace('"', "").lower()
    compact = re.sub(r"\s+", " ", normalized)
    metric_ref = f"{table.lower()}.{metric_field.lower()}"
    time_ref = f"{table.lower()}.{time_field.lower()}"
    group_ref = f"{table.lower()}.{group_field.lower()}"
    select_part = compact.split(" from ", 1)[0]
    group_by_part = ""
    if " group by " in compact:
        group_by_part = compact.split(" group by ", 1)[1]
        for terminator in (" order by ", " limit ", " having "):
            group_by_part = group_by_part.split(terminator, 1)[0]
    return (
        "avg(" in compact
        and metric_ref in compact
        and time_ref in select_part
        and group_ref in select_part
        and time_ref in group_by_part
        and group_ref in group_by_part
        and "group by" in compact
    )


def _grouped_avg_sql_mentions_expected_fields(
    sql: str,
    *,
    table: str,
    metric_field: str,
    group_field: str,
    group_table: str | None = None,
) -> bool:
    normalized = sql.replace("`", "").replace('"', "").lower()
    compact = re.sub(r"\s+", " ", normalized)
    metric_ref = f"{table.lower()}.{metric_field.lower()}"
    group_ref = f"{(group_table or table).lower()}.{group_field.lower()}"
    select_part = compact.split(" from ", 1)[0]
    group_by_part = ""
    if " group by " in compact:
        group_by_part = compact.split(" group by ", 1)[1]
        for terminator in (" order by ", " limit ", " having "):
            group_by_part = group_by_part.split(terminator, 1)[0]
    return (
        "avg(" in compact
        and metric_ref in compact
        and group_ref in select_part
        and group_ref in group_by_part
        and "group by" in compact
        and ("order by" in compact or "limit" in compact)
    )


def _sql_mentions_expected_join(
    sql: str,
    *,
    left_table: str,
    left_field: str,
    right_table: str,
    right_field: str,
) -> bool:
    normalized = sql.replace("`", "").replace('"', "").lower()
    compact = re.sub(r"\s+", " ", normalized)
    left_ref = f"{left_table.lower()}.{left_field.lower()}"
    right_ref = f"{right_table.lower()}.{right_field.lower()}"
    return " join " in compact and left_ref in compact and right_ref in compact


def _sql_mentions_expected_join_path(sql: str, *, question: dict[str, Any]) -> bool:
    join_path = question.get("expected_join_path")
    if isinstance(join_path, list) and join_path:
        for step in join_path:
            if not isinstance(step, dict):
                return False
            if not _sql_mentions_expected_join(
                sql,
                left_table=str(step.get("left_table") or ""),
                left_field=str(step.get("left_field") or ""),
                right_table=str(step.get("right_table") or ""),
                right_field=str(step.get("right_field") or ""),
            ):
                return False
        return True
    return _sql_mentions_expected_join(
        sql,
        left_table=str(question.get("expected_join_table") or ""),
        left_field=str(question.get("expected_join_field") or ""),
        right_table=str(question.get("expected_join_ref_table") or ""),
        right_field=str(question.get("expected_join_ref_field") or ""),
    )


def _sql_mentions_expected_filter(
    sql: str,
    *,
    table: str,
    field: str,
    value: str,
) -> bool:
    normalized = sql.replace("`", "").replace('"', "").lower()
    compact = re.sub(r"\s+", " ", normalized)
    expected_ref = re.escape(f"{table.lower()}.{field.lower()}")
    expected_value = re.escape(value.lower())
    return re.search(
        rf"{expected_ref}\s*=\s*(?:'{expected_value}'|{expected_value}|true)",
        compact,
    ) is not None


def _realdb_fallback_case_slug(index: int, question: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", question.lower()).strip("-")
    if not slug:
        slug = "case"
    return f"case-{index:02d}-{slug[:72]}"


def _realdb_typed_fallback_skipped_report(
    *,
    out_dir: Path,
    seed: int,
    reason: str,
    detail: str,
    engine: str = "mysql",
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "engine": engine,
        "status": "skipped",
        "seed": seed,
        "database": None,
        "database_url_redacted": None,
        "out_dir": str(out_dir),
        "graph": None,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "skip_reason": reason,
        "skip_detail": detail,
        "summary": {
            "questions": 0,
            "selected": 0,
            "typed_fallback_selected": 0,
            "local_selected": 0,
            "provider_call_count": 0,
            "provider_errors": 0,
            "render_errors": 0,
            "execution_ok": 0,
            "expected_field_matches": 0,
            "result_shape_ok": 0,
            "result_shape_counts": {},
            "rows_retained_cases": 0,
            "ok": 0,
            "sample_value_rows": 0,
            "pass": False,
            "skipped": True,
        },
        "records": [],
    }


def _realdb_typed_fallback_error_report(
    *,
    out_dir: Path,
    seed: int,
    database: str | None,
    db_url: str,
    error: Exception,
    engine: str = "mysql",
) -> dict[str, Any]:
    redacted = redact_db_url(db_url)
    detail = str(error).replace(db_url, redacted)
    return {
        "schema_version": 1,
        "engine": engine,
        "status": "error",
        "seed": seed,
        "database": database,
        "database_url_redacted": redacted,
        "out_dir": str(out_dir),
        "graph": None,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "error_type": type(error).__name__,
        "error_detail": detail,
        "summary": {
            "questions": 0,
            "selected": 0,
            "typed_fallback_selected": 0,
            "local_selected": 0,
            "provider_call_count": 0,
            "provider_errors": 0,
            "render_errors": 0,
            "execution_ok": 0,
            "expected_field_matches": 0,
            "result_shape_ok": 0,
            "result_shape_counts": {},
            "rows_retained_cases": 0,
            "ok": 0,
            "sample_value_rows": 0,
            "pass": False,
            "skipped": False,
        },
        "records": [],
    }


@cli.command("realdb-shard-audit-mysql")
@click.option(
    "--db-url",
    type=str,
    default=None,
    help="Live MySQL/MariaDB server URL. Defaults to SEMSQL_MYSQL_PROBE_URL.",
)
@click.option(
    "--database",
    type=str,
    default=None,
    help="Database to audit. Defaults to the URL path.",
)
@click.option(
    "--source-root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Optional Laravel source root for config/sharding.php hints.",
)
@click.option("--out-json", type=click.Path(path_type=Path), default=None)
@click.option("--out-md", type=click.Path(path_type=Path), default=None)
@click.option(
    "--strict",
    is_flag=True,
    help="Exit non-zero unless no shard families or shard issues need review.",
)
def realdb_shard_audit_mysql_cmd(
    db_url: str | None,
    database: str | None,
    source_root: Path | None,
    out_json: Path | None,
    out_md: Path | None,
    strict: bool,
) -> None:
    """Audit MySQL/MariaDB shard-table families without reading table data."""
    report = run_mysql_sharding_audit(
        db_url=db_url,
        database=database,
        source_root=source_root,
    )
    rendered = render_mysql_sharding_audit_markdown(report)
    if out_json is not None:
        _write_json_report(out_json, report)
    if out_md is not None:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(rendered, encoding="utf-8")
    click.echo(rendered.rstrip())
    if strict and not report["summary"]["pass"]:
        raise click.ClickException("realdb shard audit mysql needs review")


@cli.command("schema-card")
@click.option(
    "--graph",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Path to a `.semsql` SemanticGraph.",
)
@click.option(
    "--include-samples",
    is_flag=True,
    help="Include non-redacted sample_values from the graph. Defaults to schema-only.",
)
@click.option("--out-json", type=click.Path(path_type=Path), default=None)
@click.option("--out-md", type=click.Path(path_type=Path), default=None)
def schema_card_cmd(
    graph: Path,
    include_samples: bool,
    out_json: Path | None,
    out_md: Path | None,
) -> None:
    """Render a compact schema summary for local/LLM planning."""
    card = build_schema_card(graph, include_samples=include_samples)
    rendered = render_schema_card_markdown(card)
    if out_json is not None:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(card, indent=2) + "\n", encoding="utf-8")
    if out_md is not None:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(rendered, encoding="utf-8")
    click.echo(rendered.rstrip())


@cli.command("llm-resolution-packet")
@click.option(
    "--graph",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Path to a `.semsql` SemanticGraph.",
)
@click.option("--question", required=True, help="Rejected natural-language question.")
@click.option(
    "--route-reason",
    default="manual_rejected",
    show_default=True,
    help="Local rejection bucket or route_reason.",
)
@click.option(
    "--query-frame-json",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Optional `semsql query --query-frame-json` payload to include.",
)
@click.option(
    "--include-samples",
    is_flag=True,
    help="Include non-redacted sample_values from the graph. Defaults to schema-only.",
)
@click.option(
    "--openai",
    "use_openai",
    is_flag=True,
    help="Call OpenAI Responses API for a structured resolution proposal.",
)
@click.option(
    "--model",
    type=str,
    default=None,
    help="OpenAI model. Defaults to SEMSQL_OPENAI_MODEL or the module default.",
)
@click.option("--out-json", type=click.Path(path_type=Path), default=None)
@click.option(
    "--out-openai-json",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional path for the OpenAI request/proposal result.",
)
def llm_resolution_packet_cmd(
    graph: Path,
    question: str,
    route_reason: str,
    query_frame_json: Path | None,
    include_samples: bool,
    use_openai: bool,
    model: str | None,
    out_json: Path | None,
    out_openai_json: Path | None,
) -> None:
    """Build a fail-closed packet for LLM-assisted rejected-query resolution."""
    query_frame = (
        _read_json_file(query_frame_json)
        if query_frame_json is not None
        else None
    )
    packet = build_rejected_query_packet(
        graph,
        question,
        route_reason=route_reason,
        query_frame=query_frame,
        include_samples=include_samples,
    )
    if out_json is not None:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(packet, indent=2) + "\n", encoding="utf-8")

    if use_openai:
        try:
            result = call_openai_resolution(packet, model=model)
        except RuntimeError as exc:
            raise click.ClickException(str(exc)) from exc
        if out_openai_json is not None:
            out_openai_json.parent.mkdir(parents=True, exist_ok=True)
            out_openai_json.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        if not result["validation"]["valid"]:
            raise click.ClickException("OpenAI proposal failed SemSQL validation")
        click.echo(json.dumps(result["proposal"], indent=2))
        return

    request_preview = build_openai_resolution_request(
        packet,
        model=model or DEFAULT_OPENAI_MODEL,
    )
    click.echo(
        json.dumps(
            {
                "packet": packet,
                "openai_request_preview": request_preview,
            },
            indent=2,
        )
    )


@cli.command("llm-resolution-packets-from-pathway")
@click.option(
    "--report-json",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Pathway benchmark JSON report.",
)
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("target/llm_resolution_packets"),
    show_default=True,
    help="Directory for per-case packet JSON files.",
)
@click.option(
    "--policy",
    type=click.Choice(["current_permissive", "frame_only", "bounded_stage3", "bound_plan"]),
    default="bound_plan",
    show_default=True,
    help="Pathway policy whose fail-closed route rows should become packets.",
)
@click.option(
    "--include-samples",
    is_flag=True,
    help="Include non-redacted graph sample_values in each SchemaCard.",
)
@click.option(
    "--max-cases",
    type=click.IntRange(min=1),
    default=None,
    help="Optional cap for quick packet smoke tests.",
)
@click.option("--out-json", type=click.Path(path_type=Path), default=None)
@click.option("--out-md", type=click.Path(path_type=Path), default=None)
def llm_resolution_packets_from_pathway_cmd(
    report_json: Path,
    out_dir: Path,
    policy: str,
    include_samples: bool,
    max_cases: int | None,
    out_json: Path | None,
    out_md: Path | None,
) -> None:
    """Build typed-resolution packets for fail-closed pathway route rows."""
    summary = build_pathway_rejected_query_packets(
        report_json,
        out_dir,
        policy=policy,
        include_samples=include_samples,
        max_cases=max_cases,
    )
    rendered = render_pathway_packet_index_markdown(summary)
    if out_json is not None:
        _write_json_report(out_json, summary)
    if out_md is not None:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(rendered, encoding="utf-8")
    click.echo(rendered.rstrip())


@cli.command("llm-resolution-validate")
@click.option(
    "--packet-json",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Saved packet from `llm-resolution-packet --out-json`.",
)
@click.option(
    "--proposal-json",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="LLM resolution proposal JSON, or an OpenAI result with `proposal`.",
)
@click.option("--out-json", type=click.Path(path_type=Path), default=None)
@click.option(
    "--strict",
    is_flag=True,
    help="Exit non-zero when the proposal does not validate.",
)
def llm_resolution_validate_cmd(
    packet_json: Path,
    proposal_json: Path,
    out_json: Path | None,
    strict: bool,
) -> None:
    """Validate a typed LLM proposal against a rejected-query packet."""
    packet_payload = _read_json_file(packet_json)
    proposal_payload = _read_json_file(proposal_json)
    packet = packet_payload.get("packet", packet_payload)
    proposal = proposal_payload.get("proposal", proposal_payload)
    validation = validate_resolution_proposal(packet, proposal)
    if out_json is not None:
        _write_json_report(out_json, validation)
    click.echo(json.dumps(validation, indent=2))
    if strict and not validation["valid"]:
        raise click.ClickException("LLM resolution proposal failed validation")


@cli.command("llm-resolution-render")
@click.option(
    "--packet-json",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Saved packet from `llm-resolution-packet --out-json`.",
)
@click.option(
    "--proposal-json",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="LLM resolution proposal JSON, or an OpenAI result with `proposal`.",
)
@click.option(
    "--dialect",
    default="sqlite",
    show_default=True,
    help="sqlglot dialect for local validation.",
)
@click.option("--out-json", type=click.Path(path_type=Path), default=None)
@click.option(
    "--strict",
    is_flag=True,
    help="Exit non-zero when the proposal cannot render and validate.",
)
def llm_resolution_render_cmd(
    packet_json: Path,
    proposal_json: Path,
    dialect: str,
    out_json: Path | None,
    strict: bool,
) -> None:
    """Render a validated typed proposal into a local SQL candidate."""
    packet_payload = _read_json_file(packet_json)
    proposal_payload = _read_json_file(proposal_json)
    packet = packet_payload.get("packet", packet_payload)
    proposal = proposal_payload.get("proposal", proposal_payload)
    result = render_resolution_proposal(packet, proposal, dialect=dialect)
    if out_json is not None:
        _write_json_report(out_json, result)
    click.echo(json.dumps(result, indent=2))
    if strict and not result["valid"]:
        raise click.ClickException("LLM resolution proposal failed render validation")


@cli.command("llm-resolution-resolve-packet")
@click.option(
    "--packet-json",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Saved rejected-query packet, including packets emitted by `semsql query --rejection-packet-json`.",
)
@click.option(
    "--proposal-json",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Optional typed proposal or provider result. Mutually exclusive with --provider.",
)
@click.option(
    "--provider",
    type=click.Choice(TYPED_PROVIDER_CHOICES),
    default=None,
    help="Opt-in typed proposal provider. The provider never supplies final SQL.",
)
@click.option(
    "--provider-base-url",
    type=str,
    default=None,
    help="Base URL for --provider openai-compatible. Appends /chat/completions unless already present.",
)
@click.option(
    "--provider-api-key-env",
    type=str,
    default=None,
    help="Environment variable containing the provider API key for --provider openai-compatible.",
)
@click.option(
    "--provider-out",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional path for raw provider result JSON.",
)
@click.option(
    "--proposal-out",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional path for the extracted typed proposal JSON.",
)
@click.option(
    "--render-out",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional path for local render/validation JSON.",
)
@click.option(
    "--model",
    type=str,
    default=None,
    help="Provider model. Defaults are provider-specific.",
)
@click.option(
    "--dialect",
    default="sqlite",
    show_default=True,
    help="sqlglot dialect for local validation.",
)
@click.option(
    "--execute-sqlite",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Optional SQLite database for read-only execution after local validation.",
)
@click.option(
    "--execute-db-url",
    type=str,
    default=None,
    help="Optional sqlite/mysql/mariadb/postgres URL for read-only execution after local validation.",
)
@click.option(
    "--execution-out",
    type=click.Path(path_type=Path),
    default=None,
    help="Optional path for bounded execution-preview JSON.",
)
@click.option("--max-rows", type=click.IntRange(min=0), default=100, show_default=True)
@click.option(
    "--discard-execution-rows",
    is_flag=True,
    help="Execute read-only SQL but retain only columns/count/truncation metadata, not row values.",
)
@click.option(
    "--exec-timeout-seconds",
    type=click.FloatRange(min=0.001),
    default=30.0,
    show_default=True,
)
@click.option("--out-json", type=click.Path(path_type=Path), default=None)
@click.option("--out-md", type=click.Path(path_type=Path), default=None)
@click.option(
    "--strict",
    is_flag=True,
    help="Exit non-zero unless a locally validated SQL candidate is selected.",
)
def llm_resolution_resolve_packet_cmd(
    packet_json: Path,
    proposal_json: Path | None,
    provider: str | None,
    provider_base_url: str | None,
    provider_api_key_env: str | None,
    provider_out: Path | None,
    proposal_out: Path | None,
    render_out: Path | None,
    model: str | None,
    dialect: str,
    execute_sqlite: Path | None,
    execute_db_url: str | None,
    execution_out: Path | None,
    max_rows: int,
    discard_execution_rows: bool,
    exec_timeout_seconds: float,
    out_json: Path | None,
    out_md: Path | None,
    strict: bool,
) -> None:
    """Resolve one product rejected-query packet through the typed fallback boundary."""
    summary = _run_llm_resolution_resolve_packet(
        packet_json=packet_json,
        proposal_json=proposal_json,
        provider=provider,
        provider_base_url=provider_base_url,
        provider_api_key_env=provider_api_key_env,
        provider_out=provider_out,
        proposal_out=proposal_out,
        render_out=render_out,
        model=model,
        dialect=dialect,
        execute_sqlite=execute_sqlite,
        execute_db_url=execute_db_url,
        execution_out=execution_out,
        max_rows=max_rows,
        retain_execution_rows=not discard_execution_rows,
        exec_timeout_seconds=exec_timeout_seconds,
    )
    rendered = _render_llm_resolution_resolve_packet_markdown(summary)
    if out_json is not None:
        _write_json_report(out_json, summary)
    if out_md is not None:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(rendered, encoding="utf-8")
    click.echo(rendered.rstrip())
    if strict and summary["selected_sql"] is None:
        detail = summary.get("provider_error") or summary.get("render_error") or "no validated SQL candidate"
        raise click.ClickException(f"packet resolution unresolved: {detail}")
    execution = summary.get("execution")
    if strict and (execute_sqlite is not None or execute_db_url is not None) and isinstance(execution, dict):
        if execution.get("status") != "ok":
            raise click.ClickException(f"packet execution failed: {execution.get('error')}")


def _run_llm_resolution_resolve_packet(
    *,
    packet_json: Path,
    proposal_json: Path | None,
    provider: str | None,
    provider_base_url: str | None = None,
    provider_api_key_env: str | None = None,
    provider_out: Path | None = None,
    proposal_out: Path | None = None,
    render_out: Path | None = None,
    model: str | None = None,
    dialect: str = "sqlite",
    clarification_choice: str | None = None,
    execute_sqlite: Path | None = None,
    execute_db_url: str | None = None,
    execution_out: Path | None = None,
    max_rows: int = 100,
    retain_execution_rows: bool = True,
    exec_timeout_seconds: float = 30.0,
) -> dict[str, object]:
    if provider is not None and proposal_json is not None:
        raise click.UsageError("--provider and --proposal-json are mutually exclusive")
    if execute_sqlite is not None and execute_db_url is not None:
        raise click.UsageError("--execute-sqlite and --execute-db-url are mutually exclusive")
    packet_payload = _read_json_file(packet_json)
    packet = packet_payload.get("packet", packet_payload)
    if not isinstance(packet, dict):
        raise click.ClickException("--packet-json must contain a packet object")
    packet = _hydrate_packet_schema_card(packet)

    provider_called = False
    provider_error: str | None = None
    provider_readiness = _typed_provider_readiness(
        provider,
        provider_base_url=provider_base_url,
        provider_api_key_env=provider_api_key_env,
        model=model,
    )
    proposal_payload: dict[str, Any] | None = None
    selected_source: str | None = None
    fallback_proposal_source: str | None = None

    if proposal_json is not None:
        loaded = _read_json_file(proposal_json)
        if not isinstance(loaded, dict):
            raise click.ClickException("--proposal-json must contain a JSON object")
        proposal_payload = loaded
        selected_source = "typed_proposal"
        fallback_proposal_source = "typed_proposal"
    elif provider is not None:
        if not provider_readiness["configured"]:
            provider_error = str(provider_readiness["skipped_reason"])
        else:
            provider_called = True
            try:
                proposal_payload = _call_typed_resolution_provider(
                    packet,
                    provider=provider,
                    model=model,
                    provider_base_url=provider_base_url,
                    provider_api_key_env=provider_api_key_env,
                )
            except RuntimeError as exc:
                provider_error = str(exc)
        if proposal_payload is not None:
            selected_source = "typed_provider"
            fallback_proposal_source = "typed_provider"
            if provider_out is not None:
                provider_out.parent.mkdir(parents=True, exist_ok=True)
                provider_out.write_text(
                    json.dumps(proposal_payload, indent=2) + "\n",
                    encoding="utf-8",
                )
    else:
        local_proposal = build_runtime_frame_resolution_proposal(packet)
        if local_proposal is not None:
            proposal_payload = {
                "source": "local_runtime_frame_resolution_task",
                "proposal": local_proposal,
            }
            selected_source = "local_runtime_frame_resolution_task"
            fallback_proposal_source = "local_runtime_frame_resolution_task"
    proposal: dict[str, Any] | None = None
    render_result: dict[str, Any] | None = None
    render_valid: bool | None = None
    render_issue_count: int | None = None
    render_issues: list[dict[str, object]] | None = None
    render_error: str | None = None
    selected_sql: str | None = None
    result_shape: dict[str, object] | None = None
    execution_result: dict[str, object] | None = None

    if proposal_payload is not None:
        maybe_proposal = proposal_payload.get("proposal", proposal_payload)
        if not isinstance(maybe_proposal, dict):
            render_error = "proposal payload must contain a JSON object"
        else:
            proposal = maybe_proposal
            if proposal_out is not None:
                proposal_out.parent.mkdir(parents=True, exist_ok=True)
                proposal_out.write_text(
                    json.dumps(proposal, indent=2) + "\n",
                    encoding="utf-8",
                )
            render_result = render_resolution_proposal(
                packet,
                proposal,
                dialect=dialect,
                clarification_choice=clarification_choice,
            )
            render_valid = bool(render_result.get("valid"))
            render_issue_count = len(render_result.get("issues", []))
            render_issues = _summarize_render_issues(render_result.get("issues"))
            rendered_sql = render_result.get("sql")
            if render_valid and isinstance(rendered_sql, str):
                selected_sql = rendered_sql
                result_shape = _result_shape_hint(selected_sql)
            if render_out is not None:
                render_out.parent.mkdir(parents=True, exist_ok=True)
                render_out.write_text(
                    json.dumps(render_result, indent=2) + "\n",
                    encoding="utf-8",
                )

    if execute_sqlite is not None or execute_db_url is not None:
        if selected_sql is None:
            execution_result = {
                "requested": True,
                "engine": "unknown",
                "target": (
                    str(execute_sqlite)
                    if execute_sqlite is not None
                    else _redact_db_url(execute_db_url or "")
                ),
                "status": "skipped",
                "error": "no locally validated SQL candidate was selected",
                "columns": [],
                "rows": [],
                "row_count_preview": 0,
                "truncated": False,
                "rows_retained": False,
            }
        elif execute_db_url is not None:
            execution_result = _execute_selected_db_url(
                execute_db_url,
                selected_sql,
                dialect=dialect,
                max_rows=max_rows,
                retain_rows=retain_execution_rows,
                timeout_seconds=exec_timeout_seconds,
            )
        else:
            assert execute_sqlite is not None
            execution_result = _execute_selected_sqlite(
                execute_sqlite,
                selected_sql,
                max_rows=max_rows,
                retain_rows=retain_execution_rows,
                timeout_seconds=exec_timeout_seconds,
            )
        if execution_out is not None:
            execution_out.parent.mkdir(parents=True, exist_ok=True)
            execution_out.write_text(
                json.dumps(execution_result, indent=2) + "\n",
                encoding="utf-8",
            )

    return {
        "schema_version": 1,
        "source": "llm_resolution_resolve_packet",
        "packet_json": str(packet_json),
        "question": packet.get("question"),
        "route_reason": packet.get("route_reason"),
        "dialect": dialect,
        "model": model,
        "provider": provider or "none",
        "provider_readiness": provider_readiness,
        "provider_called": provider_called,
        "provider_call_count": 1 if provider_called else 0,
        "provider_error": provider_error,
        "used_direct_llm_sql": False,
        "clarification_choice": clarification_choice,
        "fallback_proposal_source": fallback_proposal_source,
        "status": "selected" if selected_sql else "unresolved",
        "selected_source": selected_source if selected_sql else None,
        "selected_sql": selected_sql,
        "result_shape": result_shape,
        "render_valid": render_valid,
        "render_issue_count": render_issue_count,
        "render_error": render_error,
        "fallback_render_valid": render_valid,
        "fallback_render_issue_count": render_issue_count,
        "fallback_render_issues": render_issues,
        "fallback_render_error": render_error,
        "execution": execution_result,
        "artifacts": {
            "packet": str(packet_json),
            "provider_result": str(provider_out) if provider_out is not None and provider_out.exists() else None,
            "proposal": str(proposal_out) if proposal_out is not None and proposal_out.exists() else None,
            "render": str(render_out) if render_out is not None and render_out.exists() else None,
            "execution": str(execution_out) if execution_out is not None and execution_out.exists() else None,
        },
    }


def _hydrate_packet_schema_card(packet: dict[str, Any]) -> dict[str, Any]:
    schema_card = packet.get("schema_card")
    if not isinstance(schema_card, dict):
        return packet
    entities = schema_card.get("entities")
    if isinstance(entities, list) and entities:
        return packet
    graph_value = schema_card.get("graph")
    if not isinstance(graph_value, str) or not graph_value:
        return packet
    graph_path = Path(graph_value)
    if not graph_path.exists():
        return packet

    hydrated = build_schema_card(graph_path, include_samples=False)
    merged_schema_card = dict(hydrated)
    for key, value in schema_card.items():
        if value not in (None, [], {}):
            merged_schema_card[key] = value
    hydrated_packet = dict(packet)
    hydrated_packet["schema_card"] = merged_schema_card
    return hydrated_packet


def _execute_selected_sqlite(
    db_path: Path,
    sql: str,
    *,
    max_rows: int,
    retain_rows: bool = True,
    timeout_seconds: float,
) -> dict[str, object]:
    result: dict[str, object] = {
        "requested": True,
        "engine": "sqlite",
        "db_path": str(db_path),
        "status": "error",
        "error": None,
        "columns": [],
        "rows": [],
        "row_count_preview": 0,
        "truncated": False,
        "rows_retained": retain_rows,
        "timeout_seconds": timeout_seconds,
        "max_rows": max_rows,
    }
    try:
        _assert_single_readonly_select(sql, dialect="sqlite")
    except Exception as exc:
        result["error"] = str(exc)
        return result

    deadline = time.monotonic() + timeout_seconds
    timed_out = False

    def abort_if_timed_out() -> int:
        nonlocal timed_out
        if time.monotonic() >= deadline:
            timed_out = True
            return 1
        return 0

    uri = f"file:{db_path.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.set_progress_handler(abort_if_timed_out, 1000)
        cursor = conn.execute(sql)
        raw_rows = cursor.fetchmany(max_rows + 1)
        columns = [description[0] for description in cursor.description or []]
        truncated = len(raw_rows) > max_rows
        preview_rows = raw_rows[:max_rows]
        result.update(
            {
                "status": "ok",
                "columns": columns,
                "rows": (
                    [
                        [_json_safe_sqlite_value(value) for value in row]
                        for row in preview_rows
                    ]
                    if retain_rows
                    else []
                ),
                "row_count_preview": len(preview_rows),
                "truncated": truncated,
                "error": None,
            }
        )
        return result
    except (sqlite3.Error, Exception) as exc:
        result["error"] = "sqlite execution timed out" if timed_out else str(exc)
        return result
    finally:
        conn.set_progress_handler(None, 0)
        conn.close()


def _execute_selected_db_url(
    db_url: str,
    sql: str,
    *,
    dialect: str,
    max_rows: int,
    retain_rows: bool = True,
    timeout_seconds: float,
) -> dict[str, object]:
    parts = urlsplit(db_url)
    scheme = parts.scheme.lower()
    if scheme == "sqlite":
        result = _empty_execution_result(
            requested=True,
            engine="sqlite",
            target=_redact_db_url(db_url),
            max_rows=max_rows,
            timeout_seconds=timeout_seconds,
        )
        try:
            path = _sqlite_path_from_url(parts)
        except ValueError as exc:
            result["error"] = str(exc)
            return result
        sqlite_result = _execute_selected_sqlite(
            path,
            sql,
            max_rows=max_rows,
            retain_rows=retain_rows,
            timeout_seconds=timeout_seconds,
        )
        sqlite_result["target"] = _redact_db_url(db_url)
        sqlite_result["execution_source"] = "db_url"
        return sqlite_result
    execution_dialect = _execution_dialect_for_scheme(scheme, dialect)
    result = _empty_execution_result(
        requested=True,
        engine=scheme or "unknown",
        target=_redact_db_url(db_url),
        max_rows=max_rows,
        retain_rows=retain_rows,
        timeout_seconds=timeout_seconds,
    )
    try:
        _assert_single_readonly_select(sql, dialect=execution_dialect)
    except Exception as exc:
        result["error"] = str(exc)
        return result
    if scheme in {"mysql", "mariadb"}:
        return _execute_selected_mysql_url(
            db_url,
            sql,
            max_rows=max_rows,
            retain_rows=retain_rows,
            timeout_seconds=timeout_seconds,
            base_result=result,
        )
    if scheme in {"postgres", "postgresql"}:
        return _execute_selected_postgres_url(
            db_url,
            sql,
            max_rows=max_rows,
            retain_rows=retain_rows,
            timeout_seconds=timeout_seconds,
            base_result=result,
        )
    result["error"] = "unsupported execution URL scheme; expected sqlite, mysql, mariadb, postgres, or postgresql"
    return result


def _empty_execution_result(
    *,
    requested: bool,
    engine: str,
    target: str,
    max_rows: int,
    retain_rows: bool = True,
    timeout_seconds: float,
) -> dict[str, object]:
    return {
        "requested": requested,
        "engine": engine,
        "target": target,
        "status": "error",
        "error": None,
        "columns": [],
        "rows": [],
        "row_count_preview": 0,
        "truncated": False,
        "rows_retained": retain_rows,
        "timeout_seconds": timeout_seconds,
        "max_rows": max_rows,
    }


def _sqlite_path_from_url(parts: Any) -> Path:
    if parts.netloc and parts.netloc not in {"", "localhost"}:
        raise ValueError("sqlite execution URL must be local, e.g. sqlite:///path/to/db.sqlite")
    path = unquote(parts.path)
    if re.match(r"^/[A-Za-z]:/", path):
        path = path[1:]
    if not path:
        raise ValueError("sqlite execution URL must include a database path")
    return Path(path)


def _execution_dialect_for_scheme(scheme: str, fallback: str) -> str:
    if scheme in {"mysql", "mariadb"}:
        return "mysql"
    if scheme in {"postgres", "postgresql"}:
        return "postgres"
    if scheme == "sqlite":
        return "sqlite"
    return fallback


def _execute_selected_mysql_url(
    db_url: str,
    sql: str,
    *,
    max_rows: int,
    retain_rows: bool = True,
    timeout_seconds: float,
    base_result: dict[str, object],
) -> dict[str, object]:
    try:
        pymysql = import_module("pymysql")
    except ImportError:
        base_result["error"] = "optional driver `pymysql` is required for mysql/mariadb execution"
        return base_result
    parts = urlsplit(db_url)
    database = unquote(parts.path.lstrip("/"))
    if not parts.hostname or not parts.username or not database:
        base_result["error"] = "mysql/mariadb execution URL must include user, host, and database"
        return base_result
    conn = None
    try:
        conn = pymysql.connect(
            host=parts.hostname,
            port=parts.port or 3306,
            user=unquote(parts.username),
            password=unquote(parts.password or ""),
            database=database,
            autocommit=False,
            charset="utf8mb4",
            read_timeout=max(1, int(timeout_seconds)),
            write_timeout=max(1, int(timeout_seconds)),
        )
        with conn.cursor() as cur:
            timeout_ms = max(1, int(timeout_seconds * 1000))
            try:
                cur.execute(f"SET SESSION max_execution_time={timeout_ms}")
            except Exception as exc:
                _execution_warnings(base_result).append(
                    f"could not set mysql max_execution_time: {exc}"
                )
            cur.execute("START TRANSACTION READ ONLY")
            cur.execute(sql)
            _fill_execution_preview(base_result, cur, max_rows=max_rows, retain_rows=retain_rows)
        conn.rollback()
        return base_result
    except Exception as exc:
        base_result["error"] = str(exc)
        return base_result
    finally:
        if conn is not None:
            try:
                conn.rollback()
            except Exception as exc:
                _execution_warnings(base_result).append(f"rollback warning: {exc}")
            conn.close()


def _execute_selected_postgres_url(
    db_url: str,
    sql: str,
    *,
    max_rows: int,
    retain_rows: bool = True,
    timeout_seconds: float,
    base_result: dict[str, object],
) -> dict[str, object]:
    try:
        pg_module = import_module("psycopg")
    except ImportError:
        try:
            pg_module = import_module("psycopg2")
        except ImportError:
            base_result["error"] = "optional driver `psycopg` or `psycopg2` is required for postgres execution"
            return base_result
    conn = None
    try:
        try:
            conn = pg_module.connect(db_url, connect_timeout=max(1, int(timeout_seconds)))
        except TypeError:
            conn = pg_module.connect(db_url)
        if hasattr(conn, "autocommit"):
            conn.autocommit = False
        with conn.cursor() as cur:
            timeout_ms = max(1, int(timeout_seconds * 1000))
            cur.execute("BEGIN READ ONLY")
            cur.execute(f"SET LOCAL statement_timeout = {timeout_ms}")
            cur.execute(sql)
            _fill_execution_preview(base_result, cur, max_rows=max_rows, retain_rows=retain_rows)
        conn.rollback()
        return base_result
    except Exception as exc:
        base_result["error"] = str(exc)
        return base_result
    finally:
        if conn is not None:
            try:
                conn.rollback()
            except Exception as exc:
                _execution_warnings(base_result).append(f"rollback warning: {exc}")
            conn.close()


def _execution_warnings(result: dict[str, object]) -> list[str]:
    warnings = result.setdefault("warnings", [])
    if isinstance(warnings, list):
        return warnings
    result["warnings"] = []
    return result["warnings"]  # type: ignore[return-value]


def _fill_execution_preview(
    result: dict[str, object],
    cursor: Any,
    *,
    max_rows: int,
    retain_rows: bool = True,
) -> None:
    raw_rows = cursor.fetchmany(max_rows + 1)
    columns = [str(description[0]) for description in cursor.description or []]
    truncated = len(raw_rows) > max_rows
    preview_rows = raw_rows[:max_rows]
    result.update(
        {
            "status": "ok",
            "columns": columns,
            "rows": (
                [
                    [_json_safe_sqlite_value(value) for value in row]
                    for row in preview_rows
                ]
                if retain_rows
                else []
            ),
            "row_count_preview": len(preview_rows),
            "truncated": truncated,
            "rows_retained": retain_rows,
            "error": None,
        }
    )


def _redact_db_url(db_url: str) -> str:
    parts = urlsplit(db_url)
    if parts.password is None:
        return db_url
    user = unquote(parts.username or "")
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    netloc = f"{user}:***@{host}{port}" if user else f"***@{host}{port}"
    return parts._replace(netloc=netloc).geturl()


def _assert_single_readonly_select(sql: str, *, dialect: str) -> None:
    import sqlglot

    statements = [statement for statement in sqlglot.parse(sql, read=dialect) if statement is not None]
    if len(statements) != 1:
        raise ValueError("execution requires exactly one SQL statement")
    statement = statements[0]
    if getattr(statement, "key", "") != "select":
        raise ValueError("execution requires a read-only SELECT statement")
    disallowed = {
        "alter",
        "attach",
        "command",
        "copy",
        "create",
        "delete",
        "drop",
        "insert",
        "merge",
        "pragma",
        "truncate",
        "update",
        "vacuum",
    }
    for node in statement.walk():
        if getattr(node, "key", "") in disallowed:
            raise ValueError("execution rejected non-read-only SQL")


def _json_safe_sqlite_value(value: object) -> object:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, bytes):
        return {"bytes_hex": value.hex()}
    return str(value)


def _result_shape_hint(sql: str) -> dict[str, object]:
    items = _select_shape_items(sql)
    if not items:
        return {
            "schema_version": 1,
            "kind": "unknown",
            "default_view": "table",
            "chartjs": None,
            "reason": "select list could not be inspected",
        }
    has_group_by = _sql_has_keyword(sql, "group by")
    dimensions = [
        item
        for item in items
        if not bool(item["aggregate"]) and not bool(item["numeric_like"])
    ]
    measures = [
        item
        for item in items
        if bool(item["aggregate"]) or bool(item["numeric_like"])
    ]
    if len(items) == 1 and bool(items[0]["aggregate"]) and not has_group_by:
        return {
            "schema_version": 1,
            "kind": "scalar_metric",
            "default_view": "metric",
            "columns": [{"name": items[0]["label"], "role": "measure"}],
            "chartjs": None,
            "reason": "single aggregate without GROUP BY",
        }
    if has_group_by and len(dimensions) >= 2 and measures:
        label_index = next(
            (idx for idx, dimension in enumerate(dimensions) if bool(dimension["time_like"])),
            0,
        )
        label_dimension = dimensions[label_index]
        series_dimension = next(
            dimension
            for idx, dimension in enumerate(dimensions)
            if idx != label_index
        )
        chart_type = "line" if bool(label_dimension["time_like"]) else "bar"
        return {
            "schema_version": 1,
            "kind": "multi_series_chart",
            "default_view": "chart",
            "columns": [
                {"name": label_dimension["label"], "role": "dimension"},
                {"name": series_dimension["label"], "role": "series"},
                {"name": measures[0]["label"], "role": "measure"},
            ],
            "chartjs": {
                "type": chart_type,
                "mapping": {
                    "labels_from": label_dimension["label"],
                    "series_from": series_dimension["label"],
                    "datasets": [
                        {"label": measure["label"], "data_from": measure["label"]}
                        for measure in measures
                    ],
                },
            },
            "fallback_view": "table",
            "reason": "two grouped dimensions with one or more measures",
        }
    if has_group_by and len(dimensions) == 1 and measures:
        dimension = dimensions[0]
        chart_type = "line" if bool(dimension["time_like"]) else "bar"
        kind = "time_series_chart" if bool(dimension["time_like"]) else "categorical_chart"
        return {
            "schema_version": 1,
            "kind": kind,
            "default_view": "chart",
            "columns": [
                {"name": dimension["label"], "role": "dimension"},
                {"name": measures[0]["label"], "role": "measure"},
            ],
            "chartjs": {
                "type": chart_type,
                "mapping": {
                    "labels_from": dimension["label"],
                    "datasets": [
                        {"label": measure["label"], "data_from": measure["label"]}
                        for measure in measures
                    ],
                },
            },
            "fallback_view": "table",
            "reason": "one grouped dimension with one or more measures",
        }
    return {
        "schema_version": 1,
        "kind": "table",
        "default_view": "table",
        "columns": [
            {
                "name": item["label"],
                "role": "measure"
                if bool(item["aggregate"]) or bool(item["numeric_like"])
                else "time_dimension"
                if bool(item["time_like"])
                else "dimension",
            }
            for item in items
        ],
        "chartjs": None,
        "reason": "shape is best represented as rows",
    }


def _select_shape_items(sql: str) -> list[dict[str, object]]:
    select_clause = _top_level_select_clause(sql)
    if select_clause is None:
        return []
    items: list[dict[str, object]] = []
    for expression in _split_top_level_commas(select_clause):
        trimmed = expression.strip()
        if not trimmed:
            continue
        lower = trimmed.lower()
        aggregate = _looks_like_aggregate_expr(lower)
        label = _select_item_label(trimmed)
        label_lower = label.lower()
        time_like = _looks_like_time_dimension(lower) or _looks_like_time_dimension(label_lower)
        numeric_like = (
            aggregate
            or _looks_like_numeric_measure(lower)
            or _looks_like_numeric_measure(label_lower)
        )
        items.append(
            {
                "label": label,
                "aggregate": aggregate,
                "time_like": time_like,
                "numeric_like": numeric_like,
            }
        )
    return items


def _top_level_select_clause(sql: str) -> str | None:
    lower = sql.lower()
    select_at = lower.find("select")
    if select_at < 0:
        return None
    from_at = _find_top_level_keyword_after(sql, "from", select_at + len("select"))
    if from_at is None:
        return None
    return sql[select_at + len("select") : from_at].strip()


def _find_top_level_keyword_after(sql: str, keyword: str, start: int) -> int | None:
    lower = sql.lower()
    needle = keyword.lower()
    depth = 0
    in_single = False
    in_double = False
    in_backtick = False
    i = start
    while i < len(sql):
        char = sql[i]
        if char == "'" and not in_double and not in_backtick:
            in_single = not in_single
        elif char == '"' and not in_single and not in_backtick:
            in_double = not in_double
        elif char == "`" and not in_single and not in_double:
            in_backtick = not in_backtick
        elif char == "(" and not in_single and not in_double and not in_backtick:
            depth += 1
        elif char == ")" and not in_single and not in_double and not in_backtick and depth > 0:
            depth -= 1
        if (
            depth == 0
            and not in_single
            and not in_double
            and not in_backtick
            and lower.startswith(needle, i)
            and _keyword_boundary(lower, i, i + len(needle))
        ):
            return i
        i += 1
    return None


def _keyword_boundary(lower: str, start: int, end: int) -> bool:
    before = lower[start - 1] if start > 0 else ""
    after = lower[end] if end < len(lower) else ""
    before_ok = not before or (not before.isalnum() and before != "_")
    after_ok = not after or (not after.isalnum() and after != "_")
    return before_ok and after_ok


def _split_top_level_commas(text: str) -> list[str]:
    items: list[str] = []
    start = 0
    depth = 0
    in_single = False
    in_double = False
    in_backtick = False
    for i, char in enumerate(text):
        if char == "'" and not in_double and not in_backtick:
            in_single = not in_single
        elif char == '"' and not in_single and not in_backtick:
            in_double = not in_double
        elif char == "`" and not in_single and not in_double:
            in_backtick = not in_backtick
        elif char == "(" and not in_single and not in_double and not in_backtick:
            depth += 1
        elif char == ")" and not in_single and not in_double and not in_backtick and depth > 0:
            depth -= 1
        elif char == "," and depth == 0 and not in_single and not in_double and not in_backtick:
            items.append(text[start:i].strip())
            start = i + 1
    items.append(text[start:].strip())
    return items


def _select_item_label(expression: str) -> str:
    alias = _select_item_alias(expression)
    if alias:
        return alias
    without_quotes = expression.strip().strip('"').strip("`")
    tail = without_quotes.rsplit(".", 1)[-1]
    return tail.strip('"').strip("`").strip(")").strip()


def _select_item_alias(expression: str) -> str | None:
    matches = list(re.finditer(r"\s+as\s+", expression, flags=re.IGNORECASE))
    if not matches:
        return None
    alias = expression[matches[-1].end() :].strip().strip('"').strip("`")
    return alias or None


def _looks_like_aggregate_expr(lower: str) -> bool:
    return any(needle in lower for needle in ("count(", "sum(", "avg(", "min(", "max("))


def _looks_like_time_dimension(lower: str) -> bool:
    if "strftime" in lower:
        return True
    tokens = _shape_tokens(lower)
    return any(
        token in tokens
        for token in (
            "date",
            "month",
            "year",
            "week",
            "day",
            "created",
            "updated",
            "resolved",
            "closed",
            "opened",
            "on",
            "at",
            "timestamp",
        )
    )


def _looks_like_numeric_measure(lower: str) -> bool:
    tokens = _shape_tokens(lower)
    return any(
        token in tokens
        for token in (
            "count",
            "sum",
            "avg",
            "average",
            "amount",
            "total",
            "revenue",
            "price",
            "cost",
            "score",
            "rate",
            "ratio",
            "percent",
            "hours",
            "duration",
            "quantity",
        )
    )


def _shape_tokens(text: str) -> set[str]:
    return {token for token in re.split(r"[^a-zA-Z0-9]+", text) if token}


def _sql_has_keyword(sql: str, keyword: str) -> bool:
    return _find_top_level_keyword_after(sql, keyword, 0) is not None


def _markdown_table(columns: list[object], rows: list[object]) -> list[str]:
    if not columns:
        return []
    header = "| " + " | ".join(_markdown_cell(column) for column in columns) + " |"
    divider = "| " + " | ".join("---" for _ in columns) + " |"
    body = []
    for row in rows:
        values = row if isinstance(row, list) else []
        padded = [*values, *[""] * max(0, len(columns) - len(values))]
        body.append("| " + " | ".join(_markdown_cell(value) for value in padded[: len(columns)]) + " |")
    return [header, divider, *body]


def _markdown_cell(value: object) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def _render_llm_resolution_resolve_packet_markdown(summary: dict[str, object]) -> str:
    provider_readiness = summary.get("provider_readiness")
    provider_configured = None
    provider_missing_env = "-"
    if isinstance(provider_readiness, dict):
        provider_configured = provider_readiness.get("configured")
        missing_env = provider_readiness.get("missing_env")
        if isinstance(missing_env, list):
            provider_missing_env = ", ".join(str(item) for item in missing_env) or "-"
    lines = [
        "# LLM-Resolution Packet Resolve",
        "",
        f"- packet: `{summary['packet_json']}`",
        f"- provider: `{summary['provider']}`",
        f"- provider configured: `{provider_configured}`",
        f"- provider missing env: `{provider_missing_env}`",
        f"- provider calls: `{summary['provider_call_count']}`",
        f"- direct LLM SQL used: `{summary['used_direct_llm_sql']}`",
        f"- render valid: `{summary['render_valid']}`",
        f"- selected source: `{summary['selected_source'] or '-'}`",
        f"- status: `{summary['status']}`",
    ]
    if summary.get("selected_sql"):
        lines.extend(["", "## Selected SQL", "", "```sql", str(summary["selected_sql"]), "```"])
    shape = summary.get("result_shape")
    if isinstance(shape, dict):
        lines.extend(
            [
                "",
                "## Result Shape",
                "",
                f"- kind: `{shape.get('kind')}`",
                f"- default view: `{shape.get('default_view')}`",
                f"- reason: `{shape.get('reason')}`",
            ]
        )
        chartjs = shape.get("chartjs")
        if isinstance(chartjs, dict):
            lines.append(f"- chartjs type: `{chartjs.get('type')}`")
    execution = summary.get("execution")
    if isinstance(execution, dict):
        lines.extend(
            [
                "",
                "## Execution",
                "",
                f"- requested: `{execution.get('requested')}`",
                f"- engine: `{execution.get('engine')}`",
                f"- status: `{execution.get('status')}`",
                f"- row preview count: `{execution.get('row_count_preview')}`",
                f"- truncated: `{execution.get('truncated')}`",
                f"- rows retained: `{execution.get('rows_retained', True)}`",
                "- policy: selected SQL only after local validation; execution adapter opens a read-only transaction or connection",
            ]
        )
        target = execution.get("target") or execution.get("db_path")
        if target:
            lines.append(f"- target: `{target}`")
        if execution.get("execution_source"):
            lines.append(f"- execution source: `{execution.get('execution_source')}`")
        if execution.get("error"):
            lines.extend(["", "```text", str(execution["error"]), "```"])
        columns = execution.get("columns")
        rows = execution.get("rows")
        if isinstance(columns, list) and isinstance(rows, list) and rows:
            lines.extend(["", "### Result Preview", "", *_markdown_table(columns, rows)])
    if summary.get("provider_error"):
        lines.extend(["", "## Provider Error", "", "```text", str(summary["provider_error"]), "```"])
    if summary.get("render_error"):
        lines.extend(["", "## Render Error", "", "```text", str(summary["render_error"]), "```"])
    return "\n".join(lines) + "\n"


@cli.command("llm-resolution-render-batch")
@click.option(
    "--packet-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
    help="Directory containing `*.packet.json` files.",
)
@click.option(
    "--proposal-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Directory containing matching `*.proposal.json` files. Defaults to packet dir.",
)
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Directory for `*.render.json` files. Defaults to packet dir.",
)
@click.option(
    "--dialect",
    default="sqlite",
    show_default=True,
    help="sqlglot dialect for local validation.",
)
@click.option("--out-json", type=click.Path(path_type=Path), default=None)
@click.option("--out-md", type=click.Path(path_type=Path), default=None)
@click.option(
    "--strict",
    is_flag=True,
    help="Exit non-zero when any packet is missing a proposal or fails rendering.",
)
def llm_resolution_render_batch_cmd(
    packet_dir: Path,
    proposal_dir: Path | None,
    out_dir: Path | None,
    dialect: str,
    out_json: Path | None,
    out_md: Path | None,
    strict: bool,
) -> None:
    """Render a directory of typed proposals into local SQL candidates."""
    summary = render_resolution_proposal_batch(
        packet_dir,
        proposal_dir=proposal_dir,
        out_dir=out_dir,
        dialect=dialect,
    )
    rendered = render_resolution_batch_markdown(summary)
    if out_json is not None:
        _write_json_report(out_json, summary)
    if out_md is not None:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(rendered, encoding="utf-8")
    click.echo(rendered.rstrip())
    if strict and (
        summary["missing_proposal_count"] > 0 or summary["invalid_count"] > 0
    ):
        raise click.ClickException("LLM resolution batch failed render validation")


@cli.command("llm-resolution-safety-gate")
@click.option(
    "--summary-json",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Provider/render/fallback batch summary JSON to evaluate.",
)
@click.option(
    "--expectations-json",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Expected per-case outcomes: route, clarify, reject, or block.",
)
@click.option("--out-json", type=click.Path(path_type=Path), default=None)
@click.option("--out-md", type=click.Path(path_type=Path), default=None)
@click.option("--strict", is_flag=True, help="Exit non-zero when the safety gate fails.")
def llm_resolution_safety_gate_cmd(
    summary_json: Path,
    expectations_json: Path,
    out_json: Path | None,
    out_md: Path | None,
    strict: bool,
) -> None:
    """Evaluate mixed typed-provider safety expectations."""
    summary = json.loads(summary_json.read_text(encoding="utf-8"))
    expectations = json.loads(expectations_json.read_text(encoding="utf-8"))
    report = evaluate_resolution_safety_expectations(
        summary,
        expectations,
        summary_path=summary_json,
        expectations_path=expectations_json,
    )
    rendered = render_resolution_safety_expectations_markdown(report)
    if out_json is not None:
        _write_json_report(out_json, report)
    if out_md is not None:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(rendered, encoding="utf-8")
    click.echo(rendered.rstrip())
    if strict and not report["pass"]:
        raise click.ClickException("LLM resolution safety gate failed")


@cli.command("llm-resolution-resolve-batch")
@click.option(
    "--packet-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
    help="Directory containing `*.packet.json` files.",
)
@click.option(
    "--provider",
    type=click.Choice(TYPED_PROVIDER_CHOICES),
    required=True,
    help="Opt-in typed proposal provider. No provider is used by default.",
)
@click.option(
    "--provider-base-url",
    type=str,
    default=None,
    help="Base URL for --provider openai-compatible. Appends /chat/completions unless already present.",
)
@click.option(
    "--provider-api-key-env",
    type=str,
    default=None,
    help="Environment variable containing the provider API key for --provider openai-compatible.",
)
@click.option(
    "--proposal-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Directory for `*.proposal.json` files. Defaults to packet dir.",
)
@click.option(
    "--provider-out",
    "provider_out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Directory for raw provider result JSON. Defaults to packet dir.",
)
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Directory for local `*.render.json` files. Defaults to packet dir.",
)
@click.option(
    "--model",
    type=str,
    default=None,
    help="Provider model. OpenAI defaults to SEMSQL_OPENAI_MODEL or module default.",
)
@click.option(
    "--dialect",
    default="sqlite",
    show_default=True,
    help="sqlglot dialect for local validation.",
)
@click.option(
    "--max-cases",
    type=click.IntRange(min=1),
    default=None,
    help="Optional cap for cautious provider smoke tests.",
)
@click.option(
    "--overwrite",
    is_flag=True,
    help="Call provider even when a matching proposal already exists.",
)
@click.option("--out-json", type=click.Path(path_type=Path), default=None)
@click.option("--out-md", type=click.Path(path_type=Path), default=None)
@click.option(
    "--strict",
    is_flag=True,
    help="Exit non-zero when provider output is missing, invalid, or unrendereable.",
)
def llm_resolution_resolve_batch_cmd(
    packet_dir: Path,
    provider: str,
    provider_base_url: str | None,
    provider_api_key_env: str | None,
    proposal_dir: Path | None,
    provider_out_dir: Path | None,
    out_dir: Path | None,
    model: str | None,
    dialect: str,
    max_cases: int | None,
    overwrite: bool,
    out_json: Path | None,
    out_md: Path | None,
    strict: bool,
) -> None:
    """Call a typed proposal provider and immediately local-validate results."""
    readiness = _typed_provider_readiness(
        provider,
        provider_base_url=provider_base_url,
        provider_api_key_env=provider_api_key_env,
        model=model,
    )
    if not readiness["configured"]:
        raise click.ClickException(str(readiness["skipped_reason"]))

    def resolver(packet: dict[str, Any]) -> dict[str, Any]:
        return _call_typed_resolution_provider(
            packet,
            provider=provider,
            model=model,
            provider_base_url=provider_base_url,
            provider_api_key_env=provider_api_key_env,
        )
    summary = resolve_resolution_proposal_batch(
        packet_dir,
        provider=resolver,
        provider_name=provider,
        proposal_dir=proposal_dir,
        out_dir=out_dir,
        provider_out_dir=provider_out_dir,
        dialect=dialect,
        max_cases=max_cases,
        overwrite=overwrite,
    )
    rendered = render_resolution_provider_batch_markdown(summary)
    if out_json is not None:
        _write_json_report(out_json, summary)
    if out_md is not None:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(rendered, encoding="utf-8")
    click.echo(rendered.rstrip())
    if strict and (
        summary["missing_proposal_count"] > 0
        or summary["provider_error_count"] > 0
        or summary["invalid_count"] > 0
    ):
        raise click.ClickException("LLM resolution provider batch failed")


@cli.command("llm-resolution-openai-request-batch")
@click.option(
    "--packet-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
    help="Directory containing `*.packet.json` files.",
)
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    required=True,
    help="Directory for OpenAI request preview JSON files.",
)
@click.option(
    "--model",
    type=str,
    default=DEFAULT_OPENAI_MODEL,
    show_default=True,
    help="OpenAI model to place in request previews.",
)
@click.option(
    "--max-cases",
    type=click.IntRange(min=1),
    default=None,
    help="Optional cap for cautious preview generation.",
)
@click.option("--out-json", type=click.Path(path_type=Path), default=None)
@click.option("--out-md", type=click.Path(path_type=Path), default=None)
def llm_resolution_openai_request_batch_cmd(
    packet_dir: Path,
    out_dir: Path,
    model: str,
    max_cases: int | None,
    out_json: Path | None,
    out_md: Path | None,
) -> None:
    """Build OpenAI request previews for packets without making API calls."""
    summary = build_openai_resolution_request_batch(
        packet_dir,
        out_dir,
        model=model,
        max_cases=max_cases,
    )
    rendered = render_openai_request_batch_markdown(summary)
    if out_json is not None:
        _write_json_report(out_json, summary)
    if out_md is not None:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(rendered, encoding="utf-8")
    click.echo(rendered.rstrip())


@cli.command("llm-resolution-capture-query")
@click.option(
    "--graph",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Path to a `.semsql` SemanticGraph.",
)
@click.option("--question", required=True, help="Natural-language question to route.")
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("target/llm_resolution_capture"),
    show_default=True,
    help="Directory for query-frame, rejected packet, request preview, and summary files.",
)
@click.option(
    "--semsql-bin",
    type=click.Path(path_type=Path),
    default=Path("target/debug/semsql.exe"),
    show_default=True,
    help="Path to the compiled `semsql` binary. Falls back to PATH if missing.",
)
@click.option(
    "--cascade-manifest",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Optional cascade manifest for model-backed runtime stages.",
)
@click.option(
    "--intent-yaml",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Optional intent pattern YAML passed to `semsql query`.",
)
@click.option(
    "--dialect",
    default="sqlite",
    show_default=True,
    help="SQL dialect passed to `semsql query`.",
)
@click.option(
    "--timeout-seconds",
    type=click.IntRange(min=1),
    default=30,
    show_default=True,
    help="Timeout for the local `semsql query` subprocess.",
)
@click.option(
    "--include-samples",
    is_flag=True,
    help="Include non-redacted sample_values in the fallback packet.",
)
@click.option(
    "--model",
    type=str,
    default=DEFAULT_OPENAI_MODEL,
    show_default=True,
    help="OpenAI model to place in the request preview.",
)
@click.option(
    "--proposal-json",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Optional typed proposal or provider result to render after capture.",
)
@click.option(
    "--strict-render",
    is_flag=True,
    help="Exit non-zero when --proposal-json cannot render and validate locally.",
)
@click.option("--out-json", type=click.Path(path_type=Path), default=None)
@click.option("--out-md", type=click.Path(path_type=Path), default=None)
def llm_resolution_capture_query_cmd(
    graph: Path,
    question: str,
    out_dir: Path,
    semsql_bin: Path,
    cascade_manifest: Path | None,
    intent_yaml: Path | None,
    dialect: str,
    timeout_seconds: int,
    include_samples: bool,
    model: str,
    proposal_json: Path | None,
    strict_render: bool,
    out_json: Path | None,
    out_md: Path | None,
) -> None:
    """Capture a fail-closed local query as a typed LLM-resolution packet."""
    out_dir.mkdir(parents=True, exist_ok=True)
    query_frame_path = out_dir / "query-frame.json"
    packet_path = out_dir / "rejected.packet.json"
    request_path = out_dir / "openai-request.json"
    render_path = out_dir / "render.json"
    summary_path = out_dir / "capture.json"
    summary_md_path = out_dir / "capture.md"
    for stale_path in (packet_path, request_path, render_path):
        if stale_path.exists():
            stale_path.unlink()

    try:
        result = run_cascade_query(
            semsql_bin,
            graph,
            question,
            timeout_seconds=timeout_seconds,
            cascade_manifest=cascade_manifest,
            intent_yaml=intent_yaml,
            dialect=dialect,
            query_frame_json=query_frame_path,
        )
    except OSError as exc:
        raise click.ClickException(f"failed to run semsql query: {exc}") from exc

    routed = result.sql is not None
    artifacts: dict[str, str | None] = {
        "query_frame": str(query_frame_path) if query_frame_path.exists() else None,
        "packet": None,
        "openai_request": None,
        "render": None,
        "summary_json": str(summary_path),
        "summary_markdown": str(summary_md_path),
    }
    strict_request: bool | None = None
    fallback_render_valid: bool | None = None
    fallback_render_issue_count: int | None = None
    fallback_sql: str | None = None
    packet_written = False
    request_written = False
    render_written = False
    if not routed:
        packet = build_rejected_query_packet(
            graph,
            question,
            route_reason=result.stage_pinned or "local_route_failed",
            query_frame=result.query_frame,
            include_samples=include_samples,
        )
        packet_path.write_text(json.dumps(packet, indent=2) + "\n", encoding="utf-8")
        request_preview = build_openai_resolution_request(
            packet,
            model=model or DEFAULT_OPENAI_MODEL,
        )
        request_path.write_text(
            json.dumps(request_preview, indent=2) + "\n",
            encoding="utf-8",
        )
        artifacts["packet"] = str(packet_path)
        artifacts["openai_request"] = str(request_path)
        strict_request = bool(
            request_preview.get("text", {}).get("format", {}).get("strict")
        )
        packet_written = True
        request_written = True
        if proposal_json is not None:
            proposal_payload = _read_json_file(proposal_json)
            proposal = proposal_payload.get("proposal", proposal_payload)
            render_result = render_resolution_proposal(
                packet,
                proposal,
                dialect=dialect,
            )
            render_path.write_text(
                json.dumps(render_result, indent=2) + "\n",
                encoding="utf-8",
            )
            artifacts["render"] = str(render_path)
            fallback_render_valid = bool(render_result.get("valid"))
            fallback_render_issue_count = len(render_result.get("issues", []))
            fallback_sql = render_result.get("sql")
            render_written = True

    summary: dict[str, object] = {
        "schema_version": 1,
        "source": "llm_resolution_capture_query",
        "graph": str(graph),
        "question": question,
        "dialect": dialect,
        "model": model,
        "include_samples": include_samples,
        "provider_call_count": 0,
        "routed_locally": routed,
        "stage_pinned": result.stage_pinned,
        "sql": result.sql,
        "error_detail": result.error_detail,
        "elapsed_seconds": result.elapsed_seconds,
        "stdout_bytes": result.stdout_bytes,
        "stderr_bytes": result.stderr_bytes,
        "query_frame_captured": query_frame_path.exists(),
        "packet_written": packet_written,
        "openai_request_written": request_written,
        "openai_request_strict": strict_request,
        "render_written": render_written,
        "fallback_render_valid": fallback_render_valid,
        "fallback_render_issue_count": fallback_render_issue_count,
        "fallback_sql": fallback_sql,
        "artifacts": artifacts,
    }
    rendered = _render_llm_resolution_capture_markdown(summary)
    _write_json_report(summary_path, summary)
    summary_md_path.write_text(rendered, encoding="utf-8")
    if out_json is not None:
        _write_json_report(out_json, summary)
    if out_md is not None:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(rendered, encoding="utf-8")
    click.echo(rendered.rstrip())
    if strict_render and proposal_json is not None and fallback_render_valid is not True:
        raise click.ClickException("captured proposal failed local render validation")


def _render_llm_resolution_capture_markdown(summary: dict[str, object]) -> str:
    artifacts = summary.get("artifacts")
    artifact_map = artifacts if isinstance(artifacts, dict) else {}
    lines = [
        "# LLM-Resolution Query Capture",
        "",
        f"- question: `{summary['question']}`",
        f"- graph: `{summary['graph']}`",
        f"- routed locally: `{summary['routed_locally']}`",
        f"- stage: `{summary['stage_pinned']}`",
        f"- provider calls: `{summary['provider_call_count']}`",
        f"- model preview: `{summary['model']}`",
        f"- query-frame captured: `{summary['query_frame_captured']}`",
        f"- packet written: `{summary['packet_written']}`",
        f"- OpenAI request written: `{summary['openai_request_written']}`",
        f"- fallback render written: `{summary['render_written']}`",
        f"- fallback render valid: `{summary['fallback_render_valid']}`",
        "",
        "## Artifacts",
        "",
    ]
    for name in (
        "query_frame",
        "packet",
        "openai_request",
        "render",
        "summary_json",
        "summary_markdown",
    ):
        value = artifact_map.get(name)
        lines.append(f"- {name}: `{value or '-'}`")
    if summary.get("fallback_sql"):
        lines.extend(
            [
                "",
                "## Fallback SQL Candidate",
                "",
                "```sql",
                str(summary["fallback_sql"]),
                "```",
            ]
        )
    if summary.get("sql"):
        lines.extend(["", "## Local SQL", "", "```sql", str(summary["sql"]), "```"])
    elif summary.get("error_detail"):
        lines.extend(["", "## Local Rejection", "", "```text", str(summary["error_detail"]), "```"])
    return "\n".join(lines) + "\n"


@cli.command("llm-resolution-fallback-query")
@click.option(
    "--graph",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Path to a `.semsql` SemanticGraph.",
)
@click.option("--question", required=True, help="Natural-language question to route.")
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("target/llm_resolution_fallback"),
    show_default=True,
    help="Directory for local/fallback query artifacts.",
)
@click.option(
    "--semsql-bin",
    type=click.Path(path_type=Path),
    default=Path("target/debug/semsql.exe"),
    show_default=True,
    help="Path to the compiled `semsql` binary. Falls back to PATH if missing.",
)
@click.option(
    "--cascade-manifest",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Optional cascade manifest for model-backed runtime stages.",
)
@click.option(
    "--intent-yaml",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Optional intent pattern YAML passed to `semsql query`.",
)
@click.option(
    "--dialect",
    default="sqlite",
    show_default=True,
    help="SQL dialect passed to `semsql query` and local fallback rendering.",
)
@click.option(
    "--execute-sqlite",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Optional SQLite database for read-only execution after local/fallback validation.",
)
@click.option(
    "--execute-db-url",
    type=str,
    default=None,
    help="Optional sqlite/mysql/mariadb/postgres URL for read-only execution after validation.",
)
@click.option("--max-rows", type=click.IntRange(min=0), default=100, show_default=True)
@click.option(
    "--discard-execution-rows",
    is_flag=True,
    help="Execute read-only SQL but retain only columns/count/truncation metadata, not row values.",
)
@click.option(
    "--exec-timeout-seconds",
    type=click.FloatRange(min=0.001),
    default=30.0,
    show_default=True,
)
@click.option(
    "--timeout-seconds",
    type=click.IntRange(min=1),
    default=30,
    show_default=True,
    help="Timeout for the local `semsql query` subprocess.",
)
@click.option(
    "--include-samples",
    is_flag=True,
    help="Include non-redacted sample_values in fallback packets.",
)
@click.option(
    "--provider",
    type=click.Choice(["none", *TYPED_PROVIDER_CHOICES]),
    default="none",
    show_default=True,
    help="Optional typed proposal provider for local rejections.",
)
@click.option(
    "--provider-base-url",
    type=str,
    default=None,
    help="Base URL for --provider openai-compatible. Appends /chat/completions unless already present.",
)
@click.option(
    "--provider-api-key-env",
    type=str,
    default=None,
    help="Environment variable containing the provider API key for --provider openai-compatible.",
)
@click.option(
    "--proposal-json",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Optional typed proposal or provider result to use instead of a provider call.",
)
@click.option(
    "--clarification-choice",
    type=str,
    default=None,
    help="Optional structured clarification option id to apply before rendering.",
)
@click.option(
    "--model",
    type=str,
    default=None,
    help="Provider model. Defaults are provider-specific.",
)
@click.option(
    "--strict",
    is_flag=True,
    help="Exit non-zero when neither local routing nor validated fallback produces SQL.",
)
@click.option("--out-json", type=click.Path(path_type=Path), default=None)
@click.option("--out-md", type=click.Path(path_type=Path), default=None)
def llm_resolution_fallback_query_cmd(
    graph: Path,
    question: str,
    out_dir: Path,
    semsql_bin: Path,
    cascade_manifest: Path | None,
    intent_yaml: Path | None,
    dialect: str,
    execute_sqlite: Path | None,
    execute_db_url: str | None,
    max_rows: int,
    discard_execution_rows: bool,
    exec_timeout_seconds: float,
    timeout_seconds: int,
    include_samples: bool,
    provider: str,
    provider_base_url: str | None,
    provider_api_key_env: str | None,
    proposal_json: Path | None,
    clarification_choice: str | None,
    model: str,
    strict: bool,
    out_json: Path | None,
    out_md: Path | None,
) -> None:
    """Route locally, then apply only a locally validated typed fallback."""
    summary = _run_llm_resolution_fallback_query(
        graph=graph,
        question=question,
        out_dir=out_dir,
        semsql_bin=semsql_bin,
        cascade_manifest=cascade_manifest,
        intent_yaml=intent_yaml,
        dialect=dialect,
        execute_sqlite=execute_sqlite,
        execute_db_url=execute_db_url,
        max_rows=max_rows,
        retain_execution_rows=not discard_execution_rows,
        exec_timeout_seconds=exec_timeout_seconds,
        timeout_seconds=timeout_seconds,
        include_samples=include_samples,
        provider=provider,
        provider_base_url=provider_base_url,
        provider_api_key_env=provider_api_key_env,
        proposal_json=proposal_json,
        clarification_choice=clarification_choice,
        model=model,
    )
    rendered = _render_llm_resolution_fallback_query_markdown(summary)
    if out_json is not None:
        _write_json_report(out_json, summary)
    if out_md is not None:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(rendered, encoding="utf-8")
    click.echo(rendered.rstrip())
    if strict and summary.get("selected_sql") is None:
        detail = (
            summary.get("provider_error")
            or summary.get("fallback_render_error")
            or "no validated SQL candidate"
        )
        raise click.ClickException(f"fallback query unresolved: {detail}")
    execution = summary.get("execution")
    if strict and (execute_sqlite is not None or execute_db_url is not None) and isinstance(execution, dict):
        if execution.get("status") != "ok":
            raise click.ClickException(f"fallback execution failed: {execution.get('error')}")


def _run_llm_resolution_fallback_query(
    *,
    graph: Path,
    question: str,
    out_dir: Path,
    semsql_bin: Path,
    cascade_manifest: Path | None,
    intent_yaml: Path | None,
    dialect: str,
    execute_sqlite: Path | None,
    execute_db_url: str | None,
    max_rows: int,
    retain_execution_rows: bool,
    exec_timeout_seconds: float,
    timeout_seconds: int,
    include_samples: bool,
    provider: str,
    provider_base_url: str | None = None,
    provider_api_key_env: str | None = None,
    proposal_json: Path | None = None,
    clarification_choice: str | None = None,
    model: str | None = None,
) -> dict[str, object]:
    if provider != "none" and proposal_json is not None:
        raise click.UsageError("--provider and --proposal-json are mutually exclusive")
    if execute_sqlite is not None and execute_db_url is not None:
        raise click.UsageError("--execute-sqlite and --execute-db-url are mutually exclusive")
    out_dir.mkdir(parents=True, exist_ok=True)
    query_frame_path = out_dir / "query-frame.json"
    packet_path = out_dir / "rejected.packet.json"
    request_path = out_dir / "openai-request.json"
    provider_path = out_dir / f"{provider}.provider.json" if provider != "none" else None
    render_path = out_dir / "render.json"
    execution_path = out_dir / "execution.json"
    summary_path = out_dir / "fallback-query.json"
    summary_md_path = out_dir / "fallback-query.md"
    stale_paths = [packet_path, request_path, render_path, execution_path]
    if provider_path is not None:
        stale_paths.append(provider_path)
    for stale_path in stale_paths:
        if stale_path.exists():
            stale_path.unlink()

    try:
        result = run_cascade_query(
            semsql_bin,
            graph,
            question,
            timeout_seconds=timeout_seconds,
            cascade_manifest=cascade_manifest,
            intent_yaml=intent_yaml,
            dialect=dialect,
            query_frame_json=query_frame_path,
        )
    except OSError as exc:
        raise click.ClickException(f"failed to run semsql query: {exc}") from exc

    selected_sql = result.sql
    selected_source = "local" if result.sql is not None else None
    provider_called = False
    provider_error: str | None = None
    provider_readiness = _typed_provider_readiness(
        provider,
        provider_base_url=provider_base_url,
        provider_api_key_env=provider_api_key_env,
        model=model,
    )
    fallback_render_valid: bool | None = None
    fallback_render_issue_count: int | None = None
    fallback_render_issues: list[dict[str, object]] | None = None
    fallback_render_error: str | None = None
    fallback_proposal_source: str | None = None
    artifacts: dict[str, str | None] = {
        "query_frame": str(query_frame_path) if query_frame_path.exists() else None,
        "packet": None,
        "openai_request": None,
        "provider_result": None,
        "render": None,
        "execution": None,
        "summary_json": str(summary_path),
        "summary_markdown": str(summary_md_path),
    }

    local_result_shape = _result_shape_hint(selected_sql) if selected_sql else None
    local_sql_rejected_reason = _local_route_shape_mismatch_reason(
        graph,
        question,
        selected_sql,
        local_result_shape,
    )
    if local_sql_rejected_reason is not None:
        selected_sql = None
        selected_source = None

    if selected_sql is None:
        packet = build_rejected_query_packet(
            graph,
            question,
            route_reason=local_sql_rejected_reason
            or result.stage_pinned
            or "local_route_failed",
            query_frame=result.query_frame,
            include_samples=include_samples,
        )
        packet_path.write_text(json.dumps(packet, indent=2) + "\n", encoding="utf-8")
        request_preview = build_openai_resolution_request(
            packet,
            model=model or DEFAULT_OPENAI_MODEL,
        )
        request_path.write_text(
            json.dumps(request_preview, indent=2) + "\n",
            encoding="utf-8",
        )
        artifacts["packet"] = str(packet_path)
        artifacts["openai_request"] = str(request_path)
        proposal_payload: dict[str, Any] | None = None
        if proposal_json is not None:
            loaded = _read_json_file(proposal_json)
            if not isinstance(loaded, dict):
                raise click.ClickException("--proposal-json must contain a JSON object")
            proposal_payload = loaded
            fallback_proposal_source = "typed_proposal"
        elif provider == "none":
            local_proposal = build_runtime_frame_resolution_proposal(packet)
            if local_proposal is not None:
                proposal_payload = {
                    "source": "local_runtime_frame_resolution_task",
                    "proposal": local_proposal,
                }
                fallback_proposal_source = "local_runtime_frame_resolution_task"
        elif provider != "none":
            if not provider_readiness["configured"]:
                provider_error = str(provider_readiness["skipped_reason"])
            else:
                provider_called = True
                try:
                    proposal_payload = _call_typed_resolution_provider(
                        packet,
                        provider=provider,
                        model=model,
                        provider_base_url=provider_base_url,
                        provider_api_key_env=provider_api_key_env,
                    )
                except RuntimeError as exc:
                    provider_error = str(exc)
            if proposal_payload is not None and provider_path is not None:
                fallback_proposal_source = "typed_provider"
                provider_path.write_text(
                    json.dumps(proposal_payload, indent=2) + "\n",
                    encoding="utf-8",
                )
                artifacts["provider_result"] = str(provider_path)

        if proposal_payload is not None:
            proposal = proposal_payload.get("proposal", proposal_payload)
            if not isinstance(proposal, dict):
                fallback_render_error = "proposal payload must contain a JSON object"
            else:
                render_result = render_resolution_proposal(
                    packet,
                    proposal,
                    dialect=dialect,
                    clarification_choice=clarification_choice,
                )
                render_path.write_text(
                    json.dumps(render_result, indent=2) + "\n",
                    encoding="utf-8",
                )
                artifacts["render"] = str(render_path)
                fallback_render_valid = bool(render_result.get("valid"))
                fallback_render_issue_count = len(render_result.get("issues", []))
                fallback_render_issues = _summarize_render_issues(
                    render_result.get("issues")
                )
                rendered_sql = render_result.get("sql")
                if fallback_render_valid and isinstance(rendered_sql, str):
                    selected_sql = rendered_sql
                    selected_source = "typed_fallback"

    result_shape = _result_shape_hint(selected_sql) if selected_sql else None
    execution_result: dict[str, object] | None = None
    if execute_sqlite is not None or execute_db_url is not None:
        if selected_sql is None:
            execution_result = {
                "requested": True,
                "engine": "unknown",
                "target": (
                    str(execute_sqlite)
                    if execute_sqlite is not None
                    else _redact_db_url(execute_db_url or "")
                ),
                "status": "skipped",
                "error": "no locally validated SQL candidate was selected",
                "columns": [],
                "rows": [],
                "row_count_preview": 0,
                "truncated": False,
                "rows_retained": False,
            }
        elif execute_db_url is not None:
            execution_result = _execute_selected_db_url(
                execute_db_url,
                selected_sql,
                dialect=dialect,
                max_rows=max_rows,
                retain_rows=retain_execution_rows,
                timeout_seconds=exec_timeout_seconds,
            )
        else:
            assert execute_sqlite is not None
            execution_result = _execute_selected_sqlite(
                execute_sqlite,
                selected_sql,
                max_rows=max_rows,
                retain_rows=retain_execution_rows,
                timeout_seconds=exec_timeout_seconds,
            )
        execution_path.write_text(json.dumps(execution_result, indent=2) + "\n", encoding="utf-8")
        artifacts["execution"] = str(execution_path)

    status = "selected" if selected_sql else "unresolved"
    summary: dict[str, object] = {
        "schema_version": 1,
        "source": "llm_resolution_fallback_query",
        "graph": str(graph),
        "question": question,
        "dialect": dialect,
        "model": model,
        "include_samples": include_samples,
        "provider": provider,
        "provider_readiness": provider_readiness,
        "provider_called": provider_called,
        "provider_call_count": 1 if provider_called else 0,
        "provider_error": provider_error,
        "used_direct_llm_sql": False,
        "clarification_choice": clarification_choice,
        "status": status,
        "selected_source": selected_source,
        "selected_sql": selected_sql,
        "result_shape": result_shape,
        "execution": execution_result,
        "local_routed": result.sql is not None,
        "local_stage_pinned": result.stage_pinned,
        "local_error_detail": result.error_detail,
        "local_sql_rejected_reason": local_sql_rejected_reason,
        "local_result_shape": local_result_shape,
        "elapsed_seconds": result.elapsed_seconds,
        "fallback_render_valid": fallback_render_valid,
        "fallback_render_issue_count": fallback_render_issue_count,
        "fallback_render_issues": fallback_render_issues,
        "fallback_render_error": fallback_render_error,
        "fallback_proposal_source": fallback_proposal_source,
        "artifacts": artifacts,
    }
    rendered = _render_llm_resolution_fallback_query_markdown(summary)
    _write_json_report(summary_path, summary)
    summary_md_path.write_text(rendered, encoding="utf-8")
    return summary


def _local_route_shape_mismatch_reason(
    graph: Path,
    question: str,
    selected_sql: str | None,
    result_shape: object,
) -> str | None:
    if not selected_sql:
        return None
    kind = (
        str(result_shape.get("kind") or "")
        if isinstance(result_shape, dict)
        else "missing"
    )
    if not _question_mentions_schema_time_dimension(graph, question):
        return None
    normalized = _normalized_question_phrase(question)
    segmented = bool(
        re.search(r"\bby\b", normalized)
        and (re.search(r"\bover\b", normalized) or "trend" in normalized)
    )
    if segmented and kind != "multi_series_chart":
        return "local_route_shape_mismatch:requested_multi_series_time_dimension"
    if not segmented and kind not in {"time_series_chart", "multi_series_chart"}:
        return "local_route_shape_mismatch:requested_time_dimension"
    return None


def _question_mentions_schema_time_dimension(graph: Path, question: str) -> bool:
    normalized = _normalized_question_phrase(question)
    if not any(marker in normalized for marker in (" over ", " trend", " by day", " by week", " by month", " by year", " by date")):
        return False
    if "over time" in normalized:
        return True
    for phrase in _graph_time_field_phrases(graph):
        if phrase and phrase in normalized:
            return True
    return False


def _graph_time_field_phrases(graph: Path) -> set[str]:
    if not graph.exists():
        return set()
    phrases: set[str] = set()
    try:
        conn = sqlite3.connect(graph)
        try:
            rows = conn.execute(
                "SELECT field, db_column, type, display_label FROM fields"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return set()
    for field, db_column, field_type, display_label in rows:
        names = [
            str(field or "").split(".", 1)[-1],
            str(db_column or ""),
            str(display_label or ""),
        ]
        if not any(
            _schema_field_ref_looks_time_like(str(name), str(field_type or ""))
            for name in names
        ):
            continue
        for name in names:
            phrase = _normalized_question_phrase(str(name).replace("_", " "))
            if phrase:
                phrases.add(phrase)
    return phrases


def _schema_field_ref_looks_time_like(name: str, field_type: str) -> bool:
    lowered_type = field_type.lower()
    if any(marker in lowered_type for marker in ("date", "time", "timestamp")):
        return True
    lowered_name = name.lower()
    return lowered_name.endswith(("_at", "_on", "_date", "_time"))


def _normalized_question_phrase(value: str) -> str:
    return f" {re.sub(r'[^a-z0-9]+', ' ', value.lower()).strip()} "


def _typed_provider_readiness(
    provider: str | None,
    *,
    provider_base_url: str | None = None,
    provider_api_key_env: str | None = None,
    model: str | None = None,
) -> dict[str, object]:
    provider_name = provider or "none"
    if provider_name == "none":
        return {
            "provider": "none",
            "configured": True,
            "missing_env": [],
            "skipped_reason": None,
        }
    if provider_name == "openai":
        missing_env = [] if os.environ.get("OPENAI_API_KEY") else ["OPENAI_API_KEY"]
        configured = not missing_env
        return {
            "provider": "openai",
            "configured": configured,
            "missing_env": missing_env,
            "skipped_reason": (
                None
                if configured
                else "provider_not_configured: OPENAI_API_KEY is not set"
            ),
        }
    if provider_name in {"openai-compatible", "groq", "deepseek"}:
        config = _typed_provider_config(
            provider_name,
            provider_base_url=provider_base_url,
            provider_api_key_env=provider_api_key_env,
            model=model,
        )
        api_key_env = str(config["api_key_env"])
        missing_env = [] if os.environ.get(api_key_env) else [api_key_env]
        missing_config = [
            name
            for name in ("base_url", "model")
            if not str(config.get(name) or "")
        ]
        configured = not missing_env and not missing_config
        if configured:
            skipped_reason = None
        else:
            details = [*missing_env, *missing_config]
            skipped_reason = (
                f"provider_not_configured: {', '.join(details)} "
                f"{'are' if len(details) != 1 else 'is'} not set"
            )
        return {
            "provider": provider_name,
            "configured": configured,
            "missing_env": missing_env,
            "missing_config": missing_config,
            "skipped_reason": skipped_reason,
            "base_url": config.get("base_url"),
            "model": config.get("model"),
        }
    return {
        "provider": provider_name,
        "configured": False,
        "missing_env": [],
        "skipped_reason": f"unsupported provider: {provider_name}",
    }


def _typed_provider_config(
    provider: str,
    *,
    provider_base_url: str | None,
    provider_api_key_env: str | None,
    model: str | None,
) -> dict[str, object]:
    if provider == "groq":
        return {
            "api_key_env": provider_api_key_env or "GROQ_API_KEY",
            "base_url": provider_base_url or os.environ.get("SEMSQL_GROQ_BASE_URL") or DEFAULT_GROQ_BASE_URL,
            "model": model or os.environ.get("SEMSQL_GROQ_MODEL") or DEFAULT_GROQ_MODEL,
            "source": "groq_chat_completions_api",
        }
    if provider == "deepseek":
        return {
            "api_key_env": provider_api_key_env or "DEEPSEEK_API_KEY",
            "base_url": provider_base_url or os.environ.get("SEMSQL_DEEPSEEK_BASE_URL") or DEFAULT_DEEPSEEK_BASE_URL,
            "model": model or os.environ.get("SEMSQL_DEEPSEEK_MODEL") or DEFAULT_DEEPSEEK_MODEL,
            "source": "deepseek_chat_completions_api",
        }
    if provider == "openai-compatible":
        return {
            "api_key_env": provider_api_key_env or "SEMSQL_OPENAI_COMPATIBLE_API_KEY",
            "base_url": provider_base_url or os.environ.get("SEMSQL_OPENAI_COMPATIBLE_BASE_URL"),
            "model": model or os.environ.get("SEMSQL_OPENAI_COMPATIBLE_MODEL"),
            "source": "openai_compatible_chat_completions_api",
        }
    return {
        "api_key_env": "",
        "base_url": "",
        "model": "",
        "source": provider,
    }


def _call_typed_resolution_provider(
    packet: dict[str, Any],
    *,
    provider: str,
    model: str | None,
    provider_base_url: str | None,
    provider_api_key_env: str | None,
) -> dict[str, Any]:
    if provider == "openai":
        return call_openai_resolution(packet, model=model)
    config = _typed_provider_config(
        provider,
        provider_base_url=provider_base_url,
        provider_api_key_env=provider_api_key_env,
        model=model,
    )
    api_key_env = str(config["api_key_env"])
    api_key = os.environ.get(api_key_env, "")
    return call_openai_chat_compatible_resolution(
        packet,
        api_key=api_key,
        base_url=str(config["base_url"]),
        model=str(config["model"]),
        source=str(config["source"]),
    )


@cli.command("llm-resolution-fallback-batch")
@click.option(
    "--packet-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
    help="Directory containing `*.packet.json` files.",
)
@click.option(
    "--proposal-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Directory containing matching `*.proposal.json` files. Defaults to packet dir.",
)
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    required=True,
    help="Directory for per-case fallback artifacts and batch summary.",
)
@click.option(
    "--semsql-bin",
    type=click.Path(path_type=Path),
    default=Path("target/debug/semsql.exe"),
    show_default=True,
    help="Path to the compiled `semsql` binary. Falls back to PATH if missing.",
)
@click.option(
    "--cascade-manifest",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Optional cascade manifest for model-backed runtime stages.",
)
@click.option(
    "--intent-yaml",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Optional intent pattern YAML passed to `semsql query`.",
)
@click.option(
    "--dialect",
    default="sqlite",
    show_default=True,
    help="SQL dialect passed to `semsql query` and local fallback rendering.",
)
@click.option(
    "--execute-sqlite",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Optional SQLite database for read-only execution after local/fallback validation.",
)
@click.option(
    "--execute-db-url",
    type=str,
    default=None,
    help="Optional sqlite/mysql/mariadb/postgres URL for read-only execution after validation.",
)
@click.option(
    "--execute-db-url-json",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Optional JSON object mapping packet stems to read-only execution DB URLs.",
)
@click.option("--max-rows", type=click.IntRange(min=0), default=100, show_default=True)
@click.option(
    "--discard-execution-rows",
    is_flag=True,
    help="Execute read-only SQL but retain only columns/count/truncation metadata, not row values.",
)
@click.option(
    "--exec-timeout-seconds",
    type=click.FloatRange(min=0.001),
    default=30.0,
    show_default=True,
)
@click.option(
    "--timeout-seconds",
    type=click.IntRange(min=1),
    default=30,
    show_default=True,
    help="Timeout for each local `semsql query` subprocess.",
)
@click.option(
    "--include-samples",
    is_flag=True,
    help="Include non-redacted sample_values in fallback packets.",
)
@click.option(
    "--provider",
    type=click.Choice(["none", *TYPED_PROVIDER_CHOICES]),
    default="none",
    show_default=True,
    help="Optional typed proposal provider for local rejections.",
)
@click.option(
    "--provider-base-url",
    type=str,
    default=None,
    help="Base URL for --provider openai-compatible. Appends /chat/completions unless already present.",
)
@click.option(
    "--provider-api-key-env",
    type=str,
    default=None,
    help="Environment variable containing the provider API key for --provider openai-compatible.",
)
@click.option(
    "--model",
    type=str,
    default=None,
    help="Provider model. Defaults are provider-specific.",
)
@click.option(
    "--clarification-choices-json",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Optional JSON object mapping packet stems to clarification option ids.",
)
@click.option(
    "--max-cases",
    type=click.IntRange(min=1),
    default=None,
    help="Optional cap for cautious batch/provider smokes.",
)
@click.option(
    "--strict",
    is_flag=True,
    help="Exit non-zero unless every packet yields local or typed-fallback SQL.",
)
@click.option("--out-json", type=click.Path(path_type=Path), default=None)
@click.option("--out-md", type=click.Path(path_type=Path), default=None)
def llm_resolution_fallback_batch_cmd(
    packet_dir: Path,
    proposal_dir: Path | None,
    out_dir: Path,
    semsql_bin: Path,
    cascade_manifest: Path | None,
    intent_yaml: Path | None,
    dialect: str,
    execute_sqlite: Path | None,
    execute_db_url: str | None,
    execute_db_url_json: Path | None,
    max_rows: int,
    discard_execution_rows: bool,
    exec_timeout_seconds: float,
    timeout_seconds: int,
    include_samples: bool,
    provider: str,
    provider_base_url: str | None,
    provider_api_key_env: str | None,
    model: str | None,
    clarification_choices_json: Path | None,
    max_cases: int | None,
    strict: bool,
    out_json: Path | None,
    out_md: Path | None,
) -> None:
    """Run local-first typed fallback selection over a packet directory."""
    summary = _run_llm_resolution_fallback_batch(
        packet_dir=packet_dir,
        proposal_dir=proposal_dir,
        out_dir=out_dir,
        semsql_bin=semsql_bin,
        cascade_manifest=cascade_manifest,
        intent_yaml=intent_yaml,
        dialect=dialect,
        execute_sqlite=execute_sqlite,
        execute_db_url=execute_db_url,
        execute_db_url_json=execute_db_url_json,
        max_rows=max_rows,
        retain_execution_rows=not discard_execution_rows,
        exec_timeout_seconds=exec_timeout_seconds,
        timeout_seconds=timeout_seconds,
        include_samples=include_samples,
        provider=provider,
        provider_base_url=provider_base_url,
        provider_api_key_env=provider_api_key_env,
        model=model,
        clarification_choices_json=clarification_choices_json,
        max_cases=max_cases,
    )
    rendered = _render_llm_resolution_fallback_batch_markdown(summary)
    summary_path = out_dir / "fallback-batch.json"
    summary_md_path = out_dir / "fallback-batch.md"
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_json_report(summary_path, summary)
    summary_md_path.write_text(rendered, encoding="utf-8")
    if out_json is not None:
        _write_json_report(out_json, summary)
    if out_md is not None:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(rendered, encoding="utf-8")
    click.echo(rendered.rstrip())
    error_count = cast(int, summary["error_count"])
    unresolved_count = cast(int, summary["unresolved_count"])
    direct_llm_sql_count = cast(int, summary["direct_llm_sql_count"])
    execution_requested = cast(bool, summary["execution_requested"])
    execution_ok_count = cast(int, summary["execution_ok_count"])
    selected_count = cast(int, summary["selected_count"])
    execution_error_count = cast(int, summary["execution_error_count"])
    execution_skipped_count = cast(int, summary["execution_skipped_count"])
    if strict and (
        error_count > 0
        or unresolved_count > 0
        or direct_llm_sql_count > 0
        or (execution_requested and execution_ok_count != selected_count)
    ):
        detail = "fallback batch did not fully select validated SQL"
        if execution_requested and execution_ok_count != selected_count:
            buckets = cast(dict[str, int], summary.get("execution_failure_buckets") or {})
            bucket_text = ", ".join(f"{key}={value}" for key, value in sorted(buckets.items()))
            detail = (
                "fallback batch execution did not pass for every selected SQL "
                f"(ok={execution_ok_count}, selected={selected_count}, "
                f"errors={execution_error_count}, skipped={execution_skipped_count}"
                f"{'; buckets: ' + bucket_text if bucket_text else ''})"
            )
        raise click.ClickException(detail)


def _run_llm_resolution_fallback_batch(
    *,
    packet_dir: Path,
    proposal_dir: Path | None,
    out_dir: Path,
    semsql_bin: Path,
    cascade_manifest: Path | None,
    intent_yaml: Path | None,
    dialect: str,
    execute_sqlite: Path | None,
    execute_db_url: str | None,
    execute_db_url_json: Path | None,
    max_rows: int,
    retain_execution_rows: bool,
    exec_timeout_seconds: float,
    timeout_seconds: int,
    include_samples: bool,
    provider: str,
    provider_base_url: str | None,
    provider_api_key_env: str | None,
    model: str | None,
    clarification_choices_json: Path | None,
    max_cases: int | None,
) -> dict[str, object]:
    if execute_sqlite is not None and execute_db_url is not None:
        raise click.UsageError("--execute-sqlite and --execute-db-url are mutually exclusive")
    if execute_sqlite is not None and execute_db_url_json is not None:
        raise click.UsageError("--execute-sqlite and --execute-db-url-json are mutually exclusive")
    resolved_proposal_dir = proposal_dir or packet_dir
    clarification_choices = _read_clarification_choice_map(clarification_choices_json)
    execution_db_urls = _read_execution_db_url_map(execute_db_url_json)
    packet_paths = sorted(packet_dir.glob("*.packet.json"))
    if max_cases is not None:
        packet_paths = packet_paths[:max_cases]
    cases: list[dict[str, object]] = []
    out_dir.mkdir(parents=True, exist_ok=True)
    for packet_path in packet_paths:
        stem = _llm_packet_stem(packet_path)
        case_out_dir = out_dir / stem
        proposal_path = resolved_proposal_dir / f"{stem}.proposal.json"
        packet_error: str | None = None
        case_summary: dict[str, object] | None = None
        try:
            case_execute_db_url = execution_db_urls.get(stem, execute_db_url)
            if execute_db_url_json is not None and case_execute_db_url is None:
                raise ValueError(
                    f"missing execution DB URL for packet stem `{stem}` in --execute-db-url-json"
                )
            packet_payload = _read_json_file(packet_path)
            if not isinstance(packet_payload, dict):
                raise ValueError("packet JSON must contain an object")
            packet = packet_payload.get("packet", packet_payload)
            if not isinstance(packet, dict):
                raise ValueError("packet payload must contain an object")
            schema_card = packet.get("schema_card")
            if not isinstance(schema_card, dict):
                raise ValueError("packet schema_card must contain an object")
            raw_graph = schema_card.get("graph")
            if not raw_graph:
                raise ValueError("packet schema_card.graph is required")
            question = packet.get("question")
            if not isinstance(question, str) or not question.strip():
                raise ValueError("packet question is required")
            selected_proposal = proposal_path if proposal_path.exists() else None
            clarification_choice = clarification_choices.get(stem)
            case_out_dir.mkdir(parents=True, exist_ok=True)
            provider_out = (
                case_out_dir / f"{provider}.provider.json"
                if provider != "none"
                else None
            )
            case_summary = _run_llm_resolution_resolve_packet(
                packet_json=packet_path,
                proposal_json=selected_proposal,
                provider=None if provider == "none" else provider,
                provider_base_url=provider_base_url,
                provider_api_key_env=provider_api_key_env,
                provider_out=provider_out,
                proposal_out=case_out_dir / "proposal.json",
                render_out=case_out_dir / "render.json",
                model=model,
                dialect=dialect,
                clarification_choice=clarification_choice,
                execute_sqlite=execute_sqlite,
                execute_db_url=case_execute_db_url,
                execution_out=case_out_dir / "execution.json",
                max_rows=max_rows,
                retain_execution_rows=retain_execution_rows,
                exec_timeout_seconds=exec_timeout_seconds,
            )
            case_summary = _adapt_packet_resolution_to_fallback_case(
                case_summary,
                graph=str(raw_graph),
                route_reason=packet.get("route_reason"),
                include_samples=include_samples,
                case_out_dir=case_out_dir,
            )
        except (OSError, ValueError, click.ClickException) as exc:
            packet_error = str(exc)
        cases.append(
            _llm_fallback_batch_case(
                stem=stem,
                packet_path=packet_path,
                proposal_path=proposal_path if proposal_path.exists() else None,
                case_out_dir=case_out_dir,
                summary=case_summary,
                error=packet_error,
            )
        )
    execution_requested = execute_sqlite is not None or execute_db_url is not None
    execution_requested = execution_requested or execute_db_url_json is not None
    execution_failure_buckets = _llm_fallback_execution_failure_buckets(cases)
    execution_failure_cases = _llm_fallback_execution_failure_cases(cases)
    return {
        "schema_version": 1,
        "source": "llm_resolution_fallback_batch",
        "packet_dir": str(packet_dir),
        "proposal_dir": str(resolved_proposal_dir),
        "out_dir": str(out_dir),
        "provider": provider,
        "model": model,
        "execution_requested": execution_requested,
        "execution_target": (
            str(execute_sqlite)
            if execute_sqlite is not None
            else (
                "per-case-db-url-map"
                if execute_db_url_json is not None
                else (_redact_db_url(execute_db_url) if execute_db_url is not None else None)
            )
        ),
        "execution_db_url_json": (
            str(execute_db_url_json) if execute_db_url_json is not None else None
        ),
        "execution_db_url_map_count": len(execution_db_urls),
        "execution_max_rows": max_rows,
        "execution_rows_retained": retain_execution_rows,
        "execution_timeout_seconds": exec_timeout_seconds,
        "clarification_choices_json": (
            str(clarification_choices_json) if clarification_choices_json is not None else None
        ),
        "clarification_choice_count": len(clarification_choices),
        "packet_count": len(cases),
        "selected_count": sum(1 for case in cases if case["status"] == "selected"),
        "unresolved_count": sum(1 for case in cases if case["status"] == "unresolved"),
        "error_count": sum(1 for case in cases if case["error"]),
        "local_selected_count": sum(
            1 for case in cases if case["selected_source"] == "local"
        ),
        "typed_fallback_selected_count": sum(
            1 for case in cases if case["selected_source"] == "typed_fallback"
        ),
        "local_runtime_frame_proposal_count": sum(
            1
            for case in cases
            if case.get("fallback_proposal_source")
            == "local_runtime_frame_resolution_task"
        ),
        "local_runtime_frame_selected_count": sum(
            1
            for case in cases
            if case.get("status") == "selected"
            and case.get("fallback_proposal_source")
            == "local_runtime_frame_resolution_task"
        ),
        "provider_call_count": sum(
            cast(int, case["provider_call_count"]) for case in cases
        ),
        "direct_llm_sql_count": sum(1 for case in cases if case["used_direct_llm_sql"]),
        "fallback_render_valid_count": sum(
            1 for case in cases if case["fallback_render_valid"] is True
        ),
        "execution_ok_count": sum(
            1 for case in cases if case.get("execution_status") == "ok"
        ),
        "execution_error_count": sum(
            1 for case in cases if case.get("execution_status") == "error"
        ),
        "execution_skipped_count": sum(
            1 for case in cases if case.get("execution_status") == "skipped"
        ),
        "execution_failure_buckets": execution_failure_buckets,
        "execution_failure_cases": execution_failure_cases,
        "rows_retained_cases": sum(
            1 for case in cases if case.get("execution_rows_retained") is True
        ),
        "cases": cases,
    }


def _adapt_packet_resolution_to_fallback_case(
    summary: dict[str, object],
    *,
    graph: str,
    route_reason: object,
    include_samples: bool,
    case_out_dir: Path,
) -> dict[str, object]:
    adapted = dict(summary)
    proposal_source = adapted.get("fallback_proposal_source")
    if adapted.get("status") == "selected":
        adapted["selected_source"] = "typed_fallback"
    adapted.update(
        {
            "source": "llm_resolution_fallback_query",
            "graph": graph,
            "include_samples": include_samples,
            "local_routed": False,
            "local_stage_pinned": "captured_packet",
            "local_error_detail": None,
            "local_sql_rejected_reason": route_reason,
            "local_result_shape": None,
            "elapsed_seconds": 0.0,
            "fallback_proposal_source": proposal_source,
        }
    )
    artifacts = dict(cast(dict[str, object], adapted.get("artifacts") or {}))
    artifacts.update(
        {
            "query_frame": None,
            "openai_request": None,
            "summary_json": str(case_out_dir / "fallback-query.json"),
            "summary_markdown": str(case_out_dir / "fallback-query.md"),
        }
    )
    adapted["artifacts"] = artifacts
    summary_path = case_out_dir / "fallback-query.json"
    summary_md_path = case_out_dir / "fallback-query.md"
    summary_path.write_text(json.dumps(adapted, indent=2) + "\n", encoding="utf-8")
    summary_md_path.write_text(
        _render_llm_resolution_fallback_query_markdown(adapted),
        encoding="utf-8",
    )
    return adapted


def _llm_fallback_batch_case(
    *,
    stem: str,
    packet_path: Path,
    proposal_path: Path | None,
    case_out_dir: Path,
    summary: dict[str, object] | None,
    error: str | None,
) -> dict[str, object]:
    if summary is None:
        return {
            "stem": stem,
            "packet_path": str(packet_path),
            "proposal_path": str(proposal_path) if proposal_path is not None else None,
            "out_dir": str(case_out_dir),
            "status": "error",
            "selected_source": None,
            "selected_sql_present": False,
            "local_routed": False,
            "provider_call_count": 0,
            "used_direct_llm_sql": False,
            "clarification_choice": None,
            "fallback_render_valid": None,
            "fallback_proposal_source": None,
            "fallback_render_issue_codes": [],
            "execution_target": None,
            "execution_status": None,
            "execution_error": None,
            "execution_failure_bucket": None,
            "execution_rows_retained": None,
            "execution_row_count_preview": None,
            "error": error or "unknown error",
        }
    issue_codes = [
        str(issue.get("code") or "")
        for issue in cast(list[dict[str, object]], summary.get("fallback_render_issues") or [])
        if issue.get("code")
    ]
    execution = summary.get("execution")
    execution_status: object = None
    execution_error: object = None
    execution_rows_retained: object = None
    execution_row_count_preview: object = None
    if isinstance(execution, dict):
        execution_status = execution.get("status")
        execution_error = execution.get("error")
        execution_rows_retained = execution.get("rows_retained")
        execution_row_count_preview = execution.get("row_count_preview")
    execution_target = (
        execution.get("target") or execution.get("db_path")
        if isinstance(execution, dict)
        else None
    )
    return {
        "stem": stem,
        "packet_path": str(packet_path),
        "proposal_path": str(proposal_path) if proposal_path is not None else None,
        "out_dir": str(case_out_dir),
        "status": summary.get("status"),
        "selected_source": summary.get("selected_source"),
        "selected_sql_present": bool(summary.get("selected_sql")),
        "local_routed": bool(summary.get("local_routed")),
        "provider_call_count": cast(int, summary.get("provider_call_count") or 0),
        "used_direct_llm_sql": bool(summary.get("used_direct_llm_sql")),
        "clarification_choice": summary.get("clarification_choice"),
        "fallback_render_valid": summary.get("fallback_render_valid"),
        "fallback_proposal_source": summary.get("fallback_proposal_source"),
        "fallback_render_issue_codes": issue_codes,
        "execution_target": execution_target,
        "execution_status": execution_status,
        "execution_error": execution_error,
        "execution_failure_bucket": _llm_fallback_execution_failure_bucket(
            execution_status,
            execution_error,
        ),
        "execution_rows_retained": execution_rows_retained,
        "execution_row_count_preview": execution_row_count_preview,
        "error": error,
    }


def _llm_fallback_execution_failure_buckets(
    cases: list[dict[str, object]],
) -> dict[str, int]:
    return dict(
        Counter(
            str(bucket)
            for case in cases
            if (bucket := case.get("execution_failure_bucket"))
        )
    )


def _llm_fallback_execution_failure_cases(
    cases: list[dict[str, object]],
) -> list[dict[str, object]]:
    failures: list[dict[str, object]] = []
    for case in cases:
        bucket = case.get("execution_failure_bucket")
        if not bucket:
            continue
        failures.append(
            {
                "stem": case.get("stem"),
                "bucket": bucket,
                "execution_status": case.get("execution_status"),
                "execution_error": case.get("execution_error"),
                "selected_source": case.get("selected_source"),
            }
        )
    return failures


def _llm_fallback_execution_failure_bucket(
    status: object,
    error: object,
) -> str | None:
    status_text = str(status or "")
    if status_text == "skipped":
        return "execution_skipped_no_selected_sql"
    if status_text != "error":
        return None
    error_text = str(error or "").lower()
    if not error_text:
        return "execution_error"
    if "read-only select" in error_text or "exactly one sql statement" in error_text:
        return "execution_rejected_not_readonly"
    if "unsupported execution url scheme" in error_text:
        return "execution_url_unsupported"
    if "execution url must include" in error_text or "execution url must be" in error_text:
        return "execution_url_invalid"
    if "optional driver" in error_text or "is required for" in error_text:
        return "execution_driver_missing"
    if "timed out" in error_text or "timeout" in error_text:
        return "execution_timeout"
    if (
        "no such table" in error_text
        or "unknown table" in error_text
        or "doesn't exist" in error_text
        or ("relation" in error_text and "does not exist" in error_text)
    ):
        return "execution_schema_missing"
    if (
        "no such column" in error_text
        or "unknown column" in error_text
        or ("column" in error_text and "does not exist" in error_text)
    ):
        return "execution_column_missing"
    if "syntax" in error_text or "parse" in error_text:
        return "execution_sql_syntax"
    return "execution_error"


def _render_llm_resolution_fallback_batch_markdown(summary: dict[str, object]) -> str:
    failure_buckets = cast(dict[str, int], summary.get("execution_failure_buckets") or {})
    failure_bucket_text = ", ".join(
        f"{key}={value}" for key, value in sorted(failure_buckets.items())
    )
    lines = [
        "# LLM-Resolution Fallback Batch",
        "",
        f"- packet dir: `{summary['packet_dir']}`",
        f"- proposal dir: `{summary['proposal_dir']}`",
        f"- output dir: `{summary['out_dir']}`",
        f"- provider: `{summary['provider']}`",
        f"- model: `{summary['model']}`",
        f"- execution requested: `{summary.get('execution_requested', False)}`",
        f"- execution target: `{summary.get('execution_target') or '-'}`",
        f"- execution URL map count: `{summary.get('execution_db_url_map_count', 0)}`",
        f"- clarification choices: `{summary.get('clarification_choice_count', 0)}`",
        f"- packets: `{summary['packet_count']}`",
        f"- selected: `{summary['selected_count']}`",
        f"- unresolved: `{summary['unresolved_count']}`",
        f"- errors: `{summary['error_count']}`",
        f"- local selected: `{summary['local_selected_count']}`",
        f"- typed fallback selected: `{summary['typed_fallback_selected_count']}`",
        f"- local runtime-frame proposals: `{summary.get('local_runtime_frame_proposal_count', 0)}`",
        f"- local runtime-frame selected: `{summary.get('local_runtime_frame_selected_count', 0)}`",
        f"- provider calls: `{summary['provider_call_count']}`",
        f"- direct LLM SQL used: `{summary['direct_llm_sql_count']}`",
        f"- fallback render valid: `{summary['fallback_render_valid_count']}`",
        f"- execution ok: `{summary.get('execution_ok_count', 0)}`",
        f"- execution errors: `{summary.get('execution_error_count', 0)}`",
        f"- execution skipped: `{summary.get('execution_skipped_count', 0)}`",
        f"- execution failure buckets: `{failure_bucket_text or '-'}`",
        f"- rows retained cases: `{summary.get('rows_retained_cases', 0)}`",
        "",
        "| Case | Status | Source | Choice | Provider Calls | Render Valid | Exec | Exec Bucket | Rows | Issues | Error | Exec Error |",
        "|---|---|---|---|---:|---:|---|---|---:|---|---|---|",
    ]
    for case in cast(list[dict[str, object]], summary["cases"]):
        issues = ", ".join(
            str(code) for code in cast(list[object], case.get("fallback_render_issue_codes") or [])
        )
        lines.append(
            f"| `{case['stem']}` | `{case['status']}` | "
            f"`{case['selected_source'] or '-'}` | "
            f"`{case.get('clarification_choice') or '-'}` | "
            f"`{case['provider_call_count']}` | "
            f"`{case['fallback_render_valid']}` | "
            f"`{case.get('execution_status') or '-'}` | "
            f"`{case.get('execution_failure_bucket') or '-'}` | "
            f"`{case.get('execution_row_count_preview') if case.get('execution_row_count_preview') is not None else '-'}` | "
            f"`{issues or '-'}` | "
            f"`{case['error'] or '-'}` | "
            f"`{_short_markdown_error(case.get('execution_error'))}` |"
        )
    return "\n".join(lines) + "\n"


def _short_markdown_error(error: object, *, max_len: int = 120) -> str:
    if error in (None, ""):
        return "-"
    text = str(error).replace("`", "'").replace("|", "\\|").replace("\n", " ")
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _read_clarification_choice_map(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    payload = _read_json_file(path)
    if not isinstance(payload, dict):
        raise click.ClickException("--clarification-choices-json must contain a JSON object")
    choices: dict[str, str] = {}
    for key, value in payload.items():
        if not isinstance(value, str) or not value.strip():
            raise click.ClickException(
                "--clarification-choices-json values must be non-empty strings"
            )
        choices[str(key)] = value.strip()
    return choices


def _read_execution_db_url_map(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    payload = _read_json_file(path)
    if not isinstance(payload, dict):
        raise click.ClickException("--execute-db-url-json must contain a JSON object")
    urls: dict[str, str] = {}
    for key, value in payload.items():
        if not isinstance(value, str) or not value.strip():
            raise click.ClickException(
                "--execute-db-url-json values must be non-empty strings"
            )
        urls[str(key)] = value.strip()
    return urls


def _summarize_render_issues(issues: object) -> list[dict[str, object]]:
    if not isinstance(issues, list):
        return []
    summarized: list[dict[str, object]] = []
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        out: dict[str, object] = {}
        for key in (
            "level",
            "code",
            "message",
            "path",
            "questions",
            "candidate_fields",
            "candidate_relationships",
            "clarification_options",
        ):
            value = issue.get(key)
            if value not in (None, "", [], {}):
                out[key] = _ascii_issue_value(value)
        if out:
            summarized.append(out)
    return summarized[:8]


def _ascii_issue_value(value: object) -> object:
    if isinstance(value, str):
        return value.replace("\u2192", "->")
    if isinstance(value, list):
        return [_ascii_issue_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _ascii_issue_value(child) for key, child in value.items()}
    return value


def _llm_packet_stem(packet_path: Path) -> str:
    suffix = ".packet.json"
    name = packet_path.name
    return name[: -len(suffix)] if name.endswith(suffix) else packet_path.stem


def _render_llm_resolution_fallback_query_markdown(summary: dict[str, object]) -> str:
    artifacts = summary.get("artifacts")
    artifact_map = artifacts if isinstance(artifacts, dict) else {}
    provider_readiness = summary.get("provider_readiness")
    provider_configured = None
    provider_missing_env = "-"
    if isinstance(provider_readiness, dict):
        provider_configured = provider_readiness.get("configured")
        missing_env = provider_readiness.get("missing_env")
        if isinstance(missing_env, list):
            provider_missing_env = ", ".join(str(item) for item in missing_env) or "-"
    lines = [
        "# LLM-Resolution Fallback Query",
        "",
        f"- question: `{summary['question']}`",
        f"- graph: `{summary['graph']}`",
        f"- status: `{summary['status']}`",
        f"- selected source: `{summary['selected_source']}`",
        f"- local routed: `{summary['local_routed']}`",
        f"- local stage: `{summary['local_stage_pinned']}`",
        f"- provider: `{summary['provider']}`",
        f"- provider configured: `{provider_configured}`",
        f"- provider missing env: `{provider_missing_env}`",
        f"- provider calls: `{summary['provider_call_count']}`",
        f"- direct LLM SQL used: `{summary['used_direct_llm_sql']}`",
        f"- clarification choice: `{summary.get('clarification_choice') or '-'}`",
        f"- fallback render valid: `{summary['fallback_render_valid']}`",
        "",
        "## Artifacts",
        "",
    ]
    for name in (
        "query_frame",
        "packet",
        "openai_request",
        "provider_result",
        "render",
        "execution",
        "summary_json",
        "summary_markdown",
    ):
        value = artifact_map.get(name)
        lines.append(f"- {name}: `{value or '-'}`")
    shape = summary.get("result_shape")
    if isinstance(shape, dict):
        lines.extend(
            [
                "",
                "## Result Shape",
                "",
                f"- kind: `{shape.get('kind')}`",
                f"- default view: `{shape.get('default_view')}`",
                f"- reason: `{shape.get('reason')}`",
            ]
        )
        chartjs = shape.get("chartjs")
        if isinstance(chartjs, dict):
            lines.append(f"- chartjs type: `{chartjs.get('type')}`")
    execution = summary.get("execution")
    if isinstance(execution, dict):
        lines.extend(
            [
                "",
                "## Execution",
                "",
                f"- requested: `{execution.get('requested')}`",
                f"- engine: `{execution.get('engine')}`",
                f"- status: `{execution.get('status')}`",
                f"- row preview count: `{execution.get('row_count_preview')}`",
                f"- truncated: `{execution.get('truncated')}`",
                f"- rows retained: `{execution.get('rows_retained', True)}`",
                "- policy: selected SQL only after local validation; execution adapter opens a read-only transaction or connection",
            ]
        )
        target = execution.get("target") or execution.get("db_path")
        if target:
            lines.append(f"- target: `{target}`")
        if execution.get("execution_source"):
            lines.append(f"- execution source: `{execution.get('execution_source')}`")
        if execution.get("error"):
            lines.extend(["", "```text", str(execution["error"]), "```"])
        columns = execution.get("columns")
        rows = execution.get("rows")
        if isinstance(columns, list) and isinstance(rows, list) and rows:
            lines.extend(["", "### Result Preview", "", *_markdown_table(columns, rows)])
    if summary.get("selected_sql"):
        lines.extend(
            [
                "",
                "## Selected SQL",
                "",
                "```sql",
                str(summary["selected_sql"]),
                "```",
            ]
        )
    render_issues = summary.get("fallback_render_issues")
    if isinstance(render_issues, list) and render_issues:
        lines.extend(["", "## Fallback Render Issues", ""])
        for issue in render_issues:
            if not isinstance(issue, dict):
                continue
            code = issue.get("code") or "unknown"
            message = issue.get("message") or ""
            lines.append(f"- `{code}`: {message}")
            questions = issue.get("questions")
            if isinstance(questions, list) and questions:
                lines.append(
                    "  questions: "
                    + "; ".join(f"`{question}`" for question in questions)
                )
    if summary.get("provider_error"):
        lines.extend(["", "## Provider Error", "", "```text", str(summary["provider_error"]), "```"])
    elif summary.get("fallback_render_error"):
        lines.extend(
            ["", "## Fallback Render Error", "", "```text", str(summary["fallback_render_error"]), "```"]
        )
    elif summary.get("local_error_detail") and not summary.get("selected_sql"):
        lines.extend(["", "## Local Rejection", "", "```text", str(summary["local_error_detail"]), "```"])
    return "\n".join(lines) + "\n"


@cli.command("queryframe-canary-suite")
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("target/queryframe_canary_suite"),
    help="Output directory for the generated canary matrix.",
)
@click.option(
    "--seed",
    "seeds",
    type=int,
    multiple=True,
    default=(20260601, 20260602, 20260603),
    show_default=True,
    help="Seed to include. Repeat for multiple seeds.",
)
@click.option(
    "--variant",
    "variants",
    type=click.Choice(["commerce", "alias", "random_alias"]),
    multiple=True,
    default=("commerce", "alias", "random_alias"),
    show_default=True,
    help="Schema naming variant to include. Repeat for multiple variants.",
)
@click.option(
    "--semsql-bin",
    type=click.Path(path_type=Path),
    default=Path("target/debug/semsql.exe"),
    help="Path to the compiled `semsql` binary. Falls back to PATH if missing.",
)
@click.option(
    "--graph-cache-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Optional graph-cache directory. Defaults under --out.",
)
@click.option(
    "--cascade-manifest",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Optional cascade manifest forwarded to every `semsql query` call.",
)
@click.option(
    "--intent-yaml",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Optional intent library YAML forwarded to every `semsql query` call.",
)
@click.option(
    "--query-timeout-seconds",
    type=click.IntRange(min=1),
    default=30,
    show_default=True,
)
@click.option(
    "--extract-timeout-seconds",
    type=click.IntRange(min=1),
    default=60,
    show_default=True,
)
@click.option(
    "--exec-timeout-seconds",
    type=click.FloatRange(min=0.001),
    default=10.0,
    show_default=True,
)
@click.option("--out-json", type=click.Path(path_type=Path), default=None)
@click.option("--out-md", type=click.Path(path_type=Path), default=None)
@click.option(
    "--strict",
    is_flag=True,
    help="Exit non-zero unless every canary run passes.",
)
def queryframe_canary_suite_cmd(
    out_dir: Path,
    seeds: tuple[int, ...],
    variants: tuple[str, ...],
    semsql_bin: Path,
    graph_cache_dir: Path | None,
    cascade_manifest: Path | None,
    intent_yaml: Path | None,
    query_timeout_seconds: int,
    extract_timeout_seconds: int,
    exec_timeout_seconds: float,
    out_json: Path | None,
    out_md: Path | None,
    strict: bool,
) -> None:
    """Run a matrix of seeded QueryFrame production canaries."""
    report = run_queryframe_canary_suite(
        out_dir=out_dir,
        seeds=seeds,
        variants=variants,
        semsql_bin=semsql_bin,
        graph_cache_dir=graph_cache_dir,
        cascade_manifest=cascade_manifest,
        intent_yaml=intent_yaml,
        query_timeout_seconds=query_timeout_seconds,
        extract_timeout_seconds=extract_timeout_seconds,
        exec_timeout_seconds=exec_timeout_seconds,
    )
    rendered = render_queryframe_canary_suite_markdown(report)
    if out_json is not None:
        _write_json_report(out_json, report)
    if out_md is not None:
        out_md.parent.mkdir(parents=True, exist_ok=True)
        out_md.write_text(rendered, encoding="utf-8")
    click.echo(rendered.rstrip())
    if strict and not report["summary"]["pass"]:
        raise click.ClickException("queryframe canary suite failed")


@cli.command("check-spider")
@click.option(
    "--root",
    "spider_root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
    help="Spider 1.0 release root — should contain dev.json, train_spider.json, "
    "tables.json, and database/<db_id>/<db_id>.sqlite per the canonical layout.",
)
@click.option(
    "--strict",
    is_flag=True,
    help="Exit non-zero on any missing artefact. Default: report and continue.",
)
def check_spider_cmd(spider_root: Path, strict: bool) -> None:
    """Verify a Spider 1.0 dataset layout.

    Spider 1.0 ships as a tarball with a fixed directory structure:

        spider/
          dev.json
          train_spider.json
          tables.json
          database/
            <db_id>/<db_id>.sqlite

    Custom mirrors (HuggingFace, derived BIRD packs) sometimes drop
    one of these or restructure the SQLite layout. This command
    walks the root, verifies each piece, and prints a punch-list so
    the user sees what's missing before running an eval that fails
    halfway through with a confusing FileNotFoundError.

    Exits 0 if every required artefact is present; non-zero with
    `--strict` on any missing piece. Without `--strict`, exits 0 even
    on warnings so CI can use the report mode independently of pass/fail.
    """
    issues: list[str] = []
    notes: list[str] = []

    # ---- top-level manifests ---------------------------------------
    for required in ("dev.json", "tables.json"):
        full = spider_root / required
        if not full.exists():
            issues.append(f"missing required manifest: {required}")
        elif full.stat().st_size == 0:
            issues.append(f"manifest is empty: {required}")
        else:
            notes.append(f"ok: {required} ({full.stat().st_size} bytes)")

    # ---- training set is optional but typically present ------------
    train = spider_root / "train_spider.json"
    if not train.exists():
        notes.append("note: train_spider.json absent (eval-only run)")

    # ---- per-DB SQLite files ---------------------------------------
    db_root = spider_root / "database"
    if not db_root.is_dir():
        issues.append("missing required directory: database/")
    else:
        # Cross-reference dev.json's referenced db_ids with the
        # filesystem so dangling-reference errors surface here.
        dev_path = spider_root / "dev.json"
        referenced: set[str] = set()
        if dev_path.exists():
            try:
                examples = json.loads(dev_path.read_text(encoding="utf-8"))
                for ex in examples:
                    if isinstance(ex, dict) and "db_id" in ex:
                        referenced.add(str(ex["db_id"]))
            except json.JSONDecodeError as e:
                issues.append(f"dev.json failed to parse: {e}")

        present: set[str] = {p.name for p in db_root.iterdir() if p.is_dir()}

        missing = sorted(referenced - present)
        for db_id in missing:
            issues.append(f"dev.json references db_id {db_id!r} but database/{db_id}/ is missing")

        # Each present db_id should have a `<db_id>.sqlite` next to
        # any per-DB schema dump.
        for db_id in sorted(present):
            sqlite = db_root / db_id / f"{db_id}.sqlite"
            if not sqlite.exists():
                issues.append(f"database/{db_id}/{db_id}.sqlite is missing")
            elif sqlite.stat().st_size == 0:
                issues.append(f"database/{db_id}/{db_id}.sqlite is empty")

        notes.append(
            f"ok: {len(present)} db dir(s), "
            f"{len(referenced)} unique db_id(s) referenced by dev.json"
        )

    # ---- output ----------------------------------------------------
    for note in notes:
        click.echo(note)
    for issue in issues:
        click.echo(f"ISSUE: {issue}", err=True)

    if issues:
        click.echo(f"\nfound {len(issues)} issue(s)")
        if strict:
            sys.exit(1)
    else:
        click.echo("\nlayout looks healthy")


@cli.command("spider2-report")
@click.option(
    "--questions",
    "questions_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Spider 2.0-lite manifest (JSON or JSONL).",
)
@click.option(
    "--out",
    "out_path",
    type=click.Path(path_type=Path),
    default=Path("docs/results/spider2-lite-report.md"),
    help="Markdown report destination. Created if absent.",
)
def spider2_report_cmd(questions_path: Path, out_path: Path) -> None:
    """Spider 2.0-lite transparent reporter (Plan §10).

    Spider 2.0-lite is out-of-scope for tiny-cascade systems — current
    SOTA is DAIL-SQL+GPT-4o at 5.68% / SFT CodeS-15B at 0.73%. We do NOT
    claim competitive numbers there. This subcommand surfaces the corpus
    statistics and the cascade's coverage profile so the README can link
    a single, honest artefact instead of running a benchmark we don't
    plan to optimise for.

    Output: a Markdown table covering corpus size, instance-id sampling,
    and Stage-0 deterministic-resolution rate. Eval execution itself
    requires the Spider 2.0-lite databases, which are out of scope here.
    """
    raw_text = questions_path.read_text(encoding="utf-8")
    # Spider 2.0-lite ships JSONL; fall back to JSON array.
    examples: list[dict[str, Any]]
    if raw_text.lstrip().startswith("["):
        examples = json.loads(raw_text)
    else:
        examples = [json.loads(line) for line in raw_text.splitlines() if line.strip()]

    by_db: dict[str, int] = {}
    instructions = 0
    for ex in examples:
        if isinstance(ex, dict):
            instructions += 1
            db = ex.get("db") or ex.get("db_id") or "?"
            by_db[str(db)] = by_db.get(str(db), 0) + 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        "# Spider 2.0-lite — Transparent Coverage Report\n\n"
        "**Position:** SemanticSQL is built for application-specific "
        "NL→SQL with a tight SemanticGraph + Intent Library. Spider 2.0-"
        "lite tests frontier reasoning over unfamiliar enterprise "
        "schemas — current SOTA is **DAIL-SQL+GPT-4o at 5.68%** "
        "(per Plan §10). Our tiny cascade is **not competitive** there "
        "by design. The optional large-LLM escalation tier is the right "
        "answer for queries that exceed Stage 0 + the schema slice.\n\n"
        f"## Corpus loaded: {questions_path}\n\n"
        f"- Total instructions: **{instructions}**\n"
        f"- Distinct databases: **{len(by_db)}**\n\n"
        "## Per-database instruction counts (top 20)\n\n"
        "| db | instructions |\n|---|---|\n"
        + "".join(
            f"| {db} | {count} |\n"
            for db, count in sorted(by_db.items(), key=lambda kv: (-kv[1], kv[0]))[:20]
        )
        + "\n\n_Generated by `semsql_eval spider2-report`._\n",
        encoding="utf-8",
    )
    click.echo(f"wrote {out_path}")


@cli.command("fetch-datasets")
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("data"),
    help="Destination root. Spider lands at <out>/spider/, BIRD at <out>/bird/.",
)
@click.option(
    "--suite",
    type=click.Choice(["spider", "bird", "all"]),
    default="all",
    help="Which suite to fetch.",
)
@click.option(
    "--with-databases",
    is_flag=True,
    help="Also download the SQLite database tarball (BIRD only — Spider DBs "
    "must be fetched manually from https://yale-lily.github.io/spider).",
)
@click.option(
    "--bird-split",
    type=click.Choice(["dev", "train", "both"]),
    default="dev",
    show_default=True,
    help="Which BIRD split to fetch/materialize.",
)
@click.option(
    "--bird-train-url",
    type=str,
    default=BIRD_TRAIN_URL,
    show_default=True,
    help="Official BIRD train.zip URL.",
)
@click.option(
    "--min-free-gb",
    type=float,
    default=DEFAULT_BIRD_TRAIN_MIN_FREE_GB,
    show_default=True,
    help="Minimum free GiB required before fetching BIRD train with databases.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Re-download archives even if cached files already exist.",
)
@click.option(
    "--keep-bird-train-archive",
    is_flag=True,
    help="Keep downloaded BIRD train.zip after successful materialization.",
)
def fetch_datasets_cmd(
    out_dir: Path,
    suite: str,
    with_databases: bool,
    bird_split: str,
    bird_train_url: str,
    min_free_gb: float,
    force: bool,
    keep_bird_train_archive: bool,
) -> None:
    """Fetch Spider 1.0 dev and BIRD dev/train splits.

    What lands on disk:

      <out>/spider/dev.json     — 1034 examples (xlangai/spider validation)
      <out>/bird/dev.json       — 1534 examples (nlile/BIRD-bench dev)
      <out>/bird/dev_databases/ — only with --with-databases
                                  (~1.5 GB; BIRD ships SQLite + CSV per DB)

    BIRD train additionally lands as ``<out>/bird/train.json`` plus
    ``<out>/bird/train_databases/`` when ``--bird-split train|both`` and
    ``--with-databases`` are supplied.

    Spider 1.0's per-database SQLite files are hosted on Google Drive,
    not HuggingFace. After running this command, fetch the database
    tarball manually from https://yale-lily.github.io/spider and unpack
    it under ``<out>/spider/database/<db_id>/<db_id>.sqlite``. Then run
    ``check-spider --root <out>/spider`` to verify the layout.

    BIRD train is distributed by the official BIRD project as ``train.zip``.
    The command requires ``--with-databases`` for train because the current
    v0.2 need is DB-backed non-dev runtime traces, not JSON-only examples.

    Idempotent: re-running re-uses the HF cache and skips already-downloaded
    splits. Network-only — air-gapped CI should mirror the splits in advance.
    """
    try:
        from huggingface_hub import hf_hub_download
    except ImportError as e:
        raise click.ClickException(
            "huggingface_hub not installed — `pip install huggingface_hub` first"
        ) from e
    try:
        import pyarrow.parquet as pq
    except ImportError as e:
        raise click.ClickException("pyarrow not installed — `pip install pyarrow` first") from e

    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = out_dir / "hf-cache"

    if suite in {"spider", "all"}:
        click.echo("→ Spider 1.0 dev (xlangai/spider validation split)")
        parquet_path = hf_hub_download(
            repo_id="xlangai/spider",
            filename="spider/validation-00000-of-00001.parquet",
            repo_type="dataset",
            cache_dir=str(cache_dir),
        )
        rows = pq.read_table(parquet_path).to_pylist()  # type: ignore[no-untyped-call]
        examples = [
            {"db_id": r["db_id"], "question": r["question"], "query": r["query"]} for r in rows
        ]
        spider_dir = out_dir / "spider"
        spider_dir.mkdir(parents=True, exist_ok=True)
        dev_path = spider_dir / "dev.json"
        dev_path.write_text(json.dumps(examples, indent=2), encoding="utf-8")
        click.echo(f"  wrote {dev_path} ({len(examples)} examples)")
        click.echo(
            "  ⚠ Spider 1.0 SQLite databases must be fetched manually from "
            "https://yale-lily.github.io/spider — extract under "
            f"{spider_dir / 'database'}"
        )

    if suite in {"bird", "all"} and bird_split in {"dev", "both"}:
        click.echo("→ BIRD dev (nlile/BIRD-bench)")
        bird_dir = out_dir / "bird"
        bird_dir.mkdir(parents=True, exist_ok=True)
        dev_json = hf_hub_download(
            repo_id="nlile/BIRD-bench",
            filename="dev.json",
            repo_type="dataset",
            cache_dir=str(cache_dir),
        )
        examples = json.loads(Path(dev_json).read_text(encoding="utf-8"))
        out_path = bird_dir / "dev.json"
        out_path.write_text(json.dumps(examples, indent=2), encoding="utf-8")
        click.echo(f"  wrote {out_path} ({len(examples)} examples)")

        if with_databases:
            click.echo("  fetching dev_databases.zip (~1.5 GB) …")
            zip_path = hf_hub_download(
                repo_id="nlile/BIRD-bench",
                filename="dev_databases.zip",
                repo_type="dataset",
                cache_dir=str(cache_dir),
            )
            safe_extract_zip(Path(zip_path), bird_dir)
            click.echo(f"  extracted databases under {bird_dir}/dev_databases/")

    if suite in {"bird", "all"} and bird_split in {"train", "both"}:
        if not with_databases:
            raise click.UsageError(
                "BIRD train fetch requires --with-databases so runtime traces "
                "can be derived against real DB contents."
            )
        bird_dir = out_dir / "bird"
        bird_dir.mkdir(parents=True, exist_ok=True)
        if bird_train_is_materialized(bird_dir) and not force:
            click.echo(f"-> BIRD train already materialized under {bird_dir}")
        else:
            try:
                ensure_min_free_space(out_dir, gibibytes(min_free_gb))
            except RuntimeError as exc:
                raise click.ClickException(str(exc)) from exc
            archive = bird_dir / "raw" / "train.zip"
            click.echo(f"-> BIRD train ({bird_train_url})")
            download_file(bird_train_url, archive, force=force)
            result = materialize_official_bird_train_archive(archive, bird_dir)
            if not keep_bird_train_archive and archive.exists():
                archive.unlink()
            click.echo(f"  wrote {result.train_json}")
            click.echo(f"  materialized databases under {result.train_databases}")

    click.echo("\ndone")


@cli.command("bypass-corpus")
@click.option(
    "--corpus",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=Path("crates/semsql-second-pass/tests/fixtures/two_parser_corpus.jsonl"),
    help="Two-parser-corpus fixture path.",
)
def bypass_corpus_cmd(corpus: Path) -> None:
    """Print a summary of the two-parser bypass corpus."""
    counts = {"positive": 0, "negative": 0}
    case_names: list[str] = []
    for line in corpus.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        case_names.append(str(rec.get("name")))
        if rec.get("should_pass"):
            counts["positive"] += 1
        else:
            counts["negative"] += 1
    click.echo(f"corpus={corpus}  positive={counts['positive']}  negative={counts['negative']}")
    for name in case_names:
        click.echo(f"  - {name}")


if __name__ == "__main__":
    cli()
    sys.exit(0)
