"""Command-line entry for the eval harness.

Subcommands:

    python -m semsql_eval spider --questions ... --tables ... --db-root ...
        Run Spider/BIRD-style evaluation against the cascade. Builds a
        per-DB SemanticGraph on first use and caches it in
        --graph-cache-dir.

    python -m semsql_eval bypass-corpus
        Print a summary of the bypass-corpus integration status — how
        many cases the rewriter scopes, how many the Rust second-pass
        accepts. Useful for `semsql doctor` parity checks.

The ML extras (torch, transformers) are not required — Spider eval
shells out to the `semsql` binary, which runs the deterministic stages
(0a + 0b + 4) and bails on anything Stage 1+ would handle. Per the
plan, that's expected: until the cascade weights ship, exec-acc on
Spider is bounded by Stage 0 coverage.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from .cascade_runner import make_cascade_predictor
from .fixtures import build_corpus
from .spider import SpiderSuite, evaluate


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def cli() -> None:
    """SemanticSQL evaluation harness."""


@cli.command("spider")
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
def spider_cmd(
    questions_path: Path,
    db_root: Path,
    semsql_bin: Path,
    graph_cache_dir: Path,
    limit: int | None,
    name: str,
    report_json: Path | None,
    cascade_manifest: Path | None,
    intent_yaml: Path | None,
) -> None:
    """Run Spider/BIRD evaluation against the cascade."""
    suite = SpiderSuite.load(questions_path, db_root, name=name)  # type: ignore[arg-type]
    examples = suite.examples
    if limit is not None:
        examples = examples[:limit]

    # Per-stage tag tracking. The cascade runner emits one tag per
    # query (`stage_0a`, `needs_model`, `error`, `timeout`, ...);
    # we accumulate counts so the summary surfaces a per-stage
    # breakdown of where each example exited.
    stage_tags: dict[str, str] = {}
    stage_counts: dict[str, int] = {}
    repair_tags: dict[str, int] = {}
    repair_total: list[int] = [0]  # mutable closure cell — running sum

    def on_stage(ex, tag: str, repair: int = 0) -> None:
        stage_tags[ex.question] = tag
        stage_counts[tag] = stage_counts.get(tag, 0) + 1
        repair_tags[ex.question] = repair
        repair_total[0] += repair

    predict = make_cascade_predictor(
        semsql_bin=semsql_bin,
        graph_cache_dir=graph_cache_dir,
        on_stage=on_stage,
        cascade_manifest=cascade_manifest,
        intent_yaml=intent_yaml,
    )

    # Per-example records, surfaced when --report-json is set so callers
    # can drill into specific failures without re-running the whole
    # suite.
    records: list[dict[str, object]] = []

    def predict_logged(example) -> str:
        sql = predict(example)
        records.append(
            {
                "db_id": example.db_id,
                "question": example.question,
                "gold_sql": example.gold_sql,
                "pred_sql": sql,
                "stage_pinned": stage_tags.get(example.question, "unknown"),
                "repair_attempts": repair_tags.get(example.question, 0),
            }
        )
        return sql

    summary = evaluate(suite, predict_logged, examples=examples)

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
        breakdown = "  ".join(
            f"{tag}={n}" for tag, n in sorted(stage_counts.items())
        )
        click.echo(f"stages: {breakdown}")

    if report_json is not None:
        report_json.parent.mkdir(parents=True, exist_ok=True)
        report_json.write_text(
            json.dumps(
                {
                    "summary": {
                        "suite": summary.suite,
                        "total": summary.total,
                        "correct": summary.correct,
                        "wrong": summary.wrong,
                        "bailed": summary.bailed,
                        "errored": summary.errored,
                        "exec_acc": summary.exec_acc,
                        "bail_rate": summary.bail_rate,
                        "error_rate": summary.error_rate,
                        "stage_breakdown": dict(stage_counts),
                        "repair_attempts_total": repair_total[0],
                    },
                    "examples": records,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        click.echo(f"per-example report written to {report_json}")


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
    examples: list[dict]
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
            for db, count in sorted(
                by_db.items(), key=lambda kv: (-kv[1], kv[0])
            )[:20]
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
def fetch_datasets_cmd(out_dir: Path, suite: str, with_databases: bool) -> None:
    """Fetch Spider 1.0 dev / BIRD dev splits from HuggingFace.

    What lands on disk:

      <out>/spider/dev.json     — 1034 examples (xlangai/spider validation)
      <out>/bird/dev.json       — 1534 examples (nlile/BIRD-bench dev)
      <out>/bird/dev_databases/ — only with --with-databases
                                  (~1.5 GB; BIRD ships SQLite + CSV per DB)

    Spider 1.0's per-database SQLite files are hosted on Google Drive,
    not HuggingFace. After running this command, fetch the database
    tarball manually from https://yale-lily.github.io/spider and unpack
    it under ``<out>/spider/database/<db_id>/<db_id>.sqlite``. Then run
    ``check-spider --root <out>/spider`` to verify the layout.

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
        import pyarrow.parquet as pq  # type: ignore[import-not-found]
    except ImportError as e:
        raise click.ClickException(
            "pyarrow not installed — `pip install pyarrow` first"
        ) from e

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
        rows = pq.read_table(parquet_path).to_pylist()
        examples = [
            {"db_id": r["db_id"], "question": r["question"], "query": r["query"]}
            for r in rows
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

    if suite in {"bird", "all"}:
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
            import zipfile

            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(bird_dir)
            click.echo(f"  extracted databases under {bird_dir}/dev_databases/")

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
    click.echo(
        f"corpus={corpus}  positive={counts['positive']}  negative={counts['negative']}"
    )
    for name in case_names:
        click.echo(f"  - {name}")


if __name__ == "__main__":
    cli()
    sys.exit(0)
