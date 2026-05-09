"""Command-line entry for the training pipeline.

Subcommands:

    python -m semsql_train spider-linker
        Build a linker JSONL corpus from a Spider/BIRD release.

    python -m semsql_train distill-linker
        Run teacher → student distillation against a JSONL corpus.

    python -m semsql_train preflight
        Validate config + data without spending GPU time.

The ML extras (torch, transformers, peft, optimum) are imported lazily —
``spider-linker`` and ``preflight`` run on a CPU-only install.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from .onnx_export import CascadeExportConfig, export_cascade, read_manifest
from .spider_linker import (
    SpiderLinkerConfig,
    generate_linker_pairs_from_spider_with_stats,
    write_pairs_jsonl,
)
from .trainers.distill import DistillConfig, distill_linker, preflight as distill_preflight
from .trainers.linker import LinkerTrainConfig, preflight as linker_preflight
from .trainers.skeleton import SkeletonTrainConfig, preflight as skeleton_preflight, train_skeleton
from .trainers.slot_filler import SlotFillerTrainConfig, preflight as slot_preflight, train_slot_filler


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def cli() -> None:
    """SemanticSQL training pipeline."""


@cli.command("spider-linker")
@click.option(
    "--questions",
    "questions_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Path to Spider/BIRD dev.json or train.json.",
)
@click.option(
    "--tables",
    "tables_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Path to Spider/BIRD tables.json.",
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(dir_okay=False, path_type=Path),
    required=True,
    help="JSONL output path.",
)
@click.option(
    "--hard-negatives", type=int, default=3, help="Hard negatives per positive."
)
@click.option(
    "--easy-negatives", type=int, default=2, help="Easy negatives per positive."
)
@click.option(
    "--dialect",
    type=str,
    default="sqlite",
    help="sqlglot dialect for gold-SQL parsing (sqlite/postgres).",
)
@click.option(
    "--seed", type=int, default=0xC0DECAFE, help="RNG seed for negative sampling."
)
def spider_linker_cmd(
    questions_path: Path,
    tables_path: Path,
    output_path: Path,
    hard_negatives: int,
    easy_negatives: int,
    dialect: str,
    seed: int,
) -> None:
    """Build linker training pairs from a Spider/BIRD release."""
    cfg = SpiderLinkerConfig(
        hard_negatives_per_positive=hard_negatives,
        easy_negatives_per_positive=easy_negatives,
        seed=seed,
    )
    records, stats = generate_linker_pairs_from_spider_with_stats(
        questions_path, tables_path, cfg, dialect=dialect
    )
    written = write_pairs_jsonl(records, output_path)
    click.echo(
        f"wrote {written} records to {output_path} "
        f"(parsed={stats['parsed']}/{stats['total_questions']} "
        f"positives={stats['positives']} negatives={stats['negatives']} "
        f"skipped_unknown_db={stats['skipped_unknown_db']} "
        f"skipped_unparseable={stats['skipped_unparseable']})"
    )


@cli.command("distill-linker")
@click.option("--train-jsonl", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--eval-jsonl", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--output-dir", type=click.Path(path_type=Path), required=True)
@click.option(
    "--teacher-model",
    type=str,
    default="cross-encoder/ms-marco-MiniLM-L-6-v2",
    help="HF model id for the teacher cross-encoder.",
)
@click.option("--student-hidden-layers", type=int, default=4)
@click.option("--student-hidden-size", type=int, default=384)
@click.option("--temperature", type=float, default=4.0)
@click.option("--alpha", type=float, default=0.7)
@click.option("--epochs", type=int, default=3)
@click.option("--batch-size", type=int, default=64)
@click.option("--learning-rate", type=float, default=2e-5)
@click.option("--seed", type=int, default=42)
@click.option("--no-fp16", "fp16", flag_value=False, default=True, type=bool)
def distill_cmd(
    train_jsonl: Path,
    eval_jsonl: Path,
    output_dir: Path,
    teacher_model: str,
    student_hidden_layers: int,
    student_hidden_size: int,
    temperature: float,
    alpha: float,
    epochs: int,
    batch_size: int,
    learning_rate: float,
    seed: int,
    fp16: bool,
) -> None:
    """Run Stage 1 distillation on a prepared JSONL corpus."""
    cfg = DistillConfig(
        train_jsonl=train_jsonl,
        eval_jsonl=eval_jsonl,
        output_dir=output_dir,
        teacher_model=teacher_model,
        student_hidden_layers=student_hidden_layers,
        student_hidden_size=student_hidden_size,
        temperature=temperature,
        alpha=alpha,
        epochs=epochs,
        batch_size=batch_size,
        learning_rate=learning_rate,
        seed=seed,
        fp16=fp16,
    )
    ok, issues = distill_preflight(cfg)
    if not ok:
        for issue in issues:
            click.echo(f"  - {issue}", err=True)
        sys.exit(1)
    report = distill_linker(cfg)
    click.echo(
        f"done: params={report.student_param_count:,} "
        f"epochs={report.epochs_completed} "
        f"final_train_loss={report.final_train_loss:.4f} "
        f"final_eval_loss={report.final_eval_loss:.4f} "
        f"recall@5={report.eval_recall_at_5:.3f} "
        f"output={report.output_dir}"
    )


@cli.command("preflight")
@click.option("--train-jsonl", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--eval-jsonl", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--output-dir", type=click.Path(path_type=Path), required=True)
def preflight_cmd(
    train_jsonl: Path, eval_jsonl: Path, output_dir: Path
) -> None:
    """Validate config + data without GPU spend.

    Runs both the linker and distill preflight checks. Exit 0 if both
    pass, 1 otherwise. Useful as a CI gate before queueing a training
    job on an expensive box.
    """
    linker_cfg = LinkerTrainConfig(
        train_jsonl=train_jsonl,
        eval_jsonl=eval_jsonl,
        output_dir=output_dir,
    )
    distill_cfg = DistillConfig(
        train_jsonl=train_jsonl,
        eval_jsonl=eval_jsonl,
        output_dir=output_dir,
    )

    lr = linker_preflight(linker_cfg)
    ok, issues = distill_preflight(distill_cfg)

    click.echo(
        f"linker: train={lr.train_count} eval={lr.eval_count} "
        f"positive_fraction={lr.positive_fraction:.2%}"
    )
    if lr.issues:
        for x in lr.issues:
            click.echo(f"  - {x}", err=True)
    if issues:
        click.echo("distill issues:", err=True)
        for x in issues:
            click.echo(f"  - {x}", err=True)
    sys.exit(0 if (lr.ok and ok) else 1)


@cli.command("export-cascade")
@click.option(
    "--output-dir",
    type=click.Path(path_type=Path),
    required=True,
    help="Directory to write ONNX artefacts + manifest.json into. Pre-existing "
    "files for stages without a supplied checkpoint are reused as-is.",
)
@click.option(
    "--cascade-version",
    type=str,
    required=True,
    help='Free-form version string written into the manifest (e.g. "v0.5.0-rc1").',
)
@click.option(
    "--linker-checkpoint",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="HF-format checkpoint dir for the Stage 1 linker. Omit to reuse the "
    "existing linker.onnx in --output-dir.",
)
@click.option(
    "--skeleton-checkpoint",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="HF-format checkpoint dir for the Stage 2 skeleton generator. Omit "
    "to reuse the existing skeleton.onnx.",
)
@click.option(
    "--slot-filler-checkpoint",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="HF-format checkpoint dir for the Stage 3 slot filler. Omit to "
    "reuse the existing slot_filler.onnx.",
)
@click.option(
    "--int8/--no-int8",
    default=True,
    help="Apply onnxruntime int8 dynamic quantisation per stage. Default: on.",
)
@click.option(
    "--opset",
    type=int,
    default=17,
    help="ONNX opset version. Pinned for cascade-runtime / server-runtime alignment.",
)
@click.option(
    "--natsql-grammar",
    type=str,
    default="natsql.lark",
    help="Grammar filename to record in the manifest.",
)
def export_cascade_cmd(
    output_dir: Path,
    cascade_version: str,
    linker_checkpoint: Path | None,
    skeleton_checkpoint: Path | None,
    slot_filler_checkpoint: Path | None,
    int8: bool,
    opset: int,
    natsql_grammar: str,
) -> None:
    """Export every stage's checkpoint to ONNX and write the manifest.

    Stage 2 milestone M4 from `docs/stage2.md`. Reuses pre-existing
    per-stage artefacts when their checkpoint is omitted, so a single
    stage can be re-exported without re-running the others.
    """
    cfg = CascadeExportConfig(
        output_dir=output_dir,
        cascade_version=cascade_version,
        linker_checkpoint=linker_checkpoint,
        skeleton_checkpoint=skeleton_checkpoint,
        slot_filler_checkpoint=slot_filler_checkpoint,
        int8=int8,
        opset=opset,
        natsql_grammar=natsql_grammar,
    )
    manifest = export_cascade(cfg)
    click.echo(
        f"wrote {output_dir / 'manifest.json'}: "
        f"linker={manifest.linker.params} params, "
        f"skeleton={manifest.skeleton.params} params, "
        f"slot_filler={manifest.slot_filler.params} params"
    )


@cli.command("inspect-manifest")
@click.option(
    "--manifest",
    "manifest_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
)
def inspect_manifest_cmd(manifest_path: Path) -> None:
    """Pretty-print a cascade manifest. Validates the schema version."""
    m = read_manifest(manifest_path)
    click.echo(f"cascade_version: {m.cascade_version}")
    click.echo(f"schema_version:  {m.schema_version}")
    click.echo(f"natsql_grammar:  {m.natsql_grammar}")
    click.echo(
        f"  linker:      {m.linker.path:<24} {m.linker.params:>12} params"
    )
    click.echo(
        f"  skeleton:    {m.skeleton.path:<24} {m.skeleton.params:>12} params"
    )
    click.echo(
        f"  slot_filler: {m.slot_filler.path:<24} {m.slot_filler.params:>12} params"
    )


@cli.command("build-teacher-cache")
@click.option(
    "--spider",
    "spider_manifest",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Spider 1.0 dev.json (or train.json). Optional.",
)
@click.option(
    "--bird",
    "bird_manifest",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="BIRD dev.json. Optional.",
)
@click.option(
    "--sqale",
    "use_sqale",
    is_flag=True,
    default=False,
    help="Stream SQaLe (trl-lab/SQaLe-text-to-SQL-dataset) instead of "
    "Spider/BIRD. Mutually exclusive with --spider/--bird. v2 default.",
)
@click.option(
    "--sqale-max-rows",
    type=int,
    default=None,
    help="Cap SQaLe ingest at N rows (smoke test). Default: stream the full split.",
)
@click.option(
    "--sqale-parquet-glob",
    type=str,
    default=None,
    help="Glob for local SQaLe parquet shards (offline-safe path). E.g. "
    "'data/hf-cache/**/train-*.parquet'. Bypasses HF datasets streaming.",
)
@click.option(
    "--omnisql",
    "use_omnisql",
    is_flag=True,
    default=False,
    help="Stream OmniSQL (RUCKBReasoning/OmniSQL-synthetic-data) instead of "
    "Spider/BIRD/SQaLe. Phase C in-distribution top-up — see "
    "docs/completion-plan.md.",
)
@click.option(
    "--omnisql-max-rows",
    type=int,
    default=None,
    help="Cap OmniSQL ingest at N rows (smoke test). Default: full split.",
)
@click.option(
    "--omnisql-parquet-glob",
    type=str,
    default=None,
    help="Glob for local OmniSQL parquet shards (offline). Bypasses HF stream.",
)
@click.option(
    "--omnisql-dataset-id",
    type=str,
    default="RUCKBReasoning/OmniSQL-synthetic-data",
    help="HF dataset id; override if the upstream repo path changes.",
)
@click.option(
    "--omnisql-question-key",
    type=str,
    default="question",
    help="Column name carrying the NL question. Override for non-OmniSQL "
    "parquets routed through this path (e.g. Gretel uses 'sql_prompt').",
)
@click.option(
    "--omnisql-sql-key",
    type=str,
    default="sql",
    help="Column name carrying gold SQL. Override per-corpus.",
)
@click.option(
    "--omnisql-db-id-key",
    type=str,
    default="db_id",
    help="Column name carrying the db_id. Override per-corpus (Gretel: 'domain').",
)
@click.option(
    "--out",
    "out_jsonl",
    type=click.Path(path_type=Path),
    required=True,
    help="Destination JSONL — Stage 2 trainer's input format.",
)
@click.option(
    "--dialect",
    type=str,
    default="sqlite",
    help="sqlglot dialect to parse gold SQL with. BIRD is mostly sqlite-compatible.",
)
def build_teacher_cache_cmd(
    spider_manifest: Path | None,
    bird_manifest: Path | None,
    use_sqale: bool,
    sqale_max_rows: int | None,
    sqale_parquet_glob: str | None,
    use_omnisql: bool,
    omnisql_max_rows: int | None,
    omnisql_parquet_glob: str | None,
    omnisql_dataset_id: str,
    omnisql_question_key: str,
    omnisql_sql_key: str,
    omnisql_db_id_key: str,
    out_jsonl: Path,
    dialect: str,
) -> None:
    """Build the Stage-2 teacher cache from gold SQL — no M2 fine-tune needed.

    Reads Spider+BIRD gold SQL, transpiles each query to a NatSQL
    skeleton with @entityN/@fieldN/@valN placeholders, and writes the
    Stage 2 trainer's expected JSONL shape. Out-of-NatSQL-v0.2 rows
    (JOINs, HAVING, CTEs, subqueries, set ops) are skipped — the run
    summary surfaces the retention rate so the operator sees the gap.

    Free, deterministic, no GPU, no API. See `docs/training-on-laptop.md`
    for why this replaces M2 entirely.
    """
    from .teacher_cache import (
        build_teacher_cache,
        build_teacher_cache_from_omnisql,
        build_teacher_cache_from_sqale,
    )

    sources_picked = sum([use_sqale, use_omnisql, bool(spider_manifest or bird_manifest)])
    if sources_picked > 1:
        raise click.UsageError(
            "--sqale / --omnisql / --spider+--bird are mutually exclusive; "
            "pick exactly one source per build"
        )
    if sources_picked == 0:
        raise click.UsageError(
            "supply one of --spider / --bird / --sqale / --omnisql"
        )

    if use_sqale:
        stats = build_teacher_cache_from_sqale(
            out_jsonl=out_jsonl,
            dialect=dialect,
            max_rows=sqale_max_rows,
            parquet_glob=sqale_parquet_glob,
        )
    elif use_omnisql:
        stats = build_teacher_cache_from_omnisql(
            out_jsonl=out_jsonl,
            dataset_id=omnisql_dataset_id,
            dialect=dialect,
            max_rows=omnisql_max_rows,
            parquet_glob=omnisql_parquet_glob,
            question_key=omnisql_question_key,
            sql_key=omnisql_sql_key,
            db_id_key=omnisql_db_id_key,
        )
    else:
        stats = build_teacher_cache(
            spider_manifest=spider_manifest,
            bird_manifest=bird_manifest,
            out_jsonl=out_jsonl,
            dialect=dialect,
        )
    click.echo(f"wrote {out_jsonl}")
    click.echo(
        f"  total={stats.total} converted={stats.converted} "
        f"retention={stats.retention:.1%}"
    )
    click.echo(
        f"  skipped: join={stats.skipped_join} subquery={stats.skipped_subquery} "
        f"having={stats.skipped_having} set_op={stats.skipped_set_op} "
        f"cte={stats.skipped_cte} parse_error={stats.skipped_parse_error} "
        f"other={stats.skipped_other}"
    )
    if stats.skip_reasons:
        click.echo(f"  first {min(5, len(stats.skip_reasons))} skip reasons:")
        for r in stats.skip_reasons[:5]:
            click.echo(f"    - {r}")


@cli.command("active-subset")
@click.option(
    "--in",
    "in_jsonl",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Source JSONL pool (e.g. the 250K synthetic-skeleton corpus).",
)
@click.option(
    "--out",
    "out_jsonl",
    type=click.Path(path_type=Path),
    required=True,
    help="Destination JSONL — diverse subset of size --target.",
)
@click.option(
    "--target",
    "target_k",
    type=int,
    required=True,
    help="Number of rows to select (recommended: 25000 per `docs/training-on-laptop.md`).",
)
@click.option(
    "--embedding-model",
    type=str,
    default="sentence-transformers/all-MiniLM-L6-v2",
    help="HF identifier for the sentence-transformer used to embed each row's NL.",
)
@click.option(
    "--seed",
    type=int,
    default=42,
    help="K-means seed. Re-runs with the same seed → identical output.",
)
def active_subset_cmd(
    in_jsonl: Path,
    out_jsonl: Path,
    target_k: int,
    embedding_model: str,
    seed: int,
) -> None:
    """Pick ``--target`` diverse rows from a synthetic pool — see
    `docs/training-on-laptop.md` §4 for why this 10× the laptop training
    speed without losing accuracy.
    """
    from .active_subset import active_subset

    stats = active_subset(
        in_jsonl=in_jsonl,
        out_jsonl=out_jsonl,
        target_k=target_k,
        embedding_model=embedding_model,
        seed=seed,
    )
    click.echo(f"wrote {out_jsonl}")
    click.echo(
        f"  pool={stats.pool_size} selected={stats.selected} "
        f"target_k={stats.target_k} skipped_no_nl={stats.skipped_no_nl}"
    )
    click.echo(f"  embedding_model={stats.embedding_model}")


@cli.command("train")
@click.option(
    "--stage",
    type=click.Choice(["linker", "skeleton", "slot-filler"]),
    required=True,
    help="Which cascade stage to train.",
)
@click.option("--train", "train_jsonl", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--eval", "eval_jsonl", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--out", "output_dir", type=click.Path(path_type=Path), required=True)
@click.option("--base-model", type=str, default=None,
              help="HF model id override. Default: t5-small (skeleton), distilbert-base-uncased (linker/slot-filler).")
@click.option("--epochs", type=int, default=None)
@click.option("--batch-size", type=int, default=None)
@click.option("--grad-accum", type=int, default=None, help="Gradient accumulation steps (skeleton only).")
@click.option("--bf16", is_flag=True, default=False, help="bf16 mixed precision (skeleton).")
@click.option("--flash-attn", "flash_attn", type=str, default=None,
              help="Attention backend: '2' or 'flash_attention_2', 'sdpa'. Skeleton only.")
@click.option("--compile", "torch_compile", is_flag=True, default=False,
              help="torch.compile the model (skeleton).")
@click.option("--liger", is_flag=True, default=False, help="Apply Liger Kernel patch (skeleton).")
@click.option(
    "--teacher-cache-mode",
    type=click.Choice(["none", "topk32", "full"]),
    default="none",
    help="Token-level KD source. 'none' = pure gold CE; 'topk32' = top-32 cached logits. Skeleton only.",
)
def train_cmd(
    stage: str,
    train_jsonl: Path,
    eval_jsonl: Path,
    output_dir: Path,
    base_model: str | None,
    epochs: int | None,
    batch_size: int | None,
    grad_accum: int | None,
    bf16: bool,
    flash_attn: str | None,
    torch_compile: bool,
    liger: bool,
    teacher_cache_mode: str,
) -> None:
    """Unified training entry for all cascade stages.

    Maps to the concrete trainer for each stage — see `docs/training-on-laptop.md`
    Phase B for the recommended flags per stage.

    Stage 1 (linker):     ~30 min on RTX 4060. recall@5 >= 95 % target.
    Stage 2 (skeleton):   ~3-4 hours. exact-skeleton >= 85 % target.
    Stage 3 (slot-filler): ~15 min. per-slot top-1 >= 90 % target.
    """
    # Normalise flash-attn shorthand: '2' → 'flash_attention_2'
    attn_impl: str | None = None
    if flash_attn is not None:
        attn_impl = "flash_attention_2" if flash_attn.strip() in ("2", "flash_attention_2") else flash_attn

    if stage == "skeleton":
        cfg = SkeletonTrainConfig(
            train_jsonl=train_jsonl,
            eval_jsonl=eval_jsonl,
            output_dir=output_dir,
            base_model=base_model or "t5-small",
            epochs=epochs or 5,
            batch_size=batch_size or 16,
            gradient_accum=grad_accum or 1,
            bf16=bf16,
            flash_attention=attn_impl,
            torch_compile=torch_compile,
            liger_kernel=liger,
        )
        report = skeleton_preflight(cfg)
        if not report.ok:
            for issue in report.issues:
                click.echo(f"  - {issue}", err=True)
            sys.exit(1)
        click.echo(
            f"preflight ok: train={report.train_count} eval={report.eval_count} "
            f"avg_skeleton_len={report.avg_skeleton_len:.1f}"
        )
        out = train_skeleton(cfg)
        click.echo(f"skeleton trainer done: output={out}")

    elif stage == "linker":
        from .trainers.linker import train_linker
        cfg_l = LinkerTrainConfig(
            train_jsonl=train_jsonl,
            eval_jsonl=eval_jsonl,
            output_dir=output_dir,
            base_model=base_model or "distilbert-base-uncased",
            epochs=epochs or 3,
            batch_size=batch_size or 64,
            bf16=bf16,
        )
        report_l = linker_preflight(cfg_l)
        if not report_l.ok:
            for issue in report_l.issues:
                click.echo(f"  - {issue}", err=True)
            sys.exit(1)
        click.echo(
            f"preflight ok: train={report_l.train_count} eval={report_l.eval_count} "
            f"positive_fraction={report_l.positive_fraction:.2%}"
        )
        out_l = train_linker(cfg_l)
        click.echo(f"linker trainer done: output={out_l}")

    else:  # slot-filler
        cfg_s = SlotFillerTrainConfig(
            train_jsonl=train_jsonl,
            eval_jsonl=eval_jsonl,
            output_dir=output_dir,
            base_model=base_model or "distilbert-base-uncased",
            epochs=epochs or 4,
            batch_size=batch_size or 64,
            bf16=bf16,
        )
        report_s = slot_preflight(cfg_s)
        if not report_s.ok:
            for issue in report_s.issues:
                click.echo(f"  - {issue}", err=True)
            sys.exit(1)
        click.echo(
            f"preflight ok: train={report_s.train_count} eval={report_s.eval_count} "
            f"avg_candidates={report_s.avg_candidates:.1f}"
        )
        out_s = train_slot_filler(cfg_s)
        click.echo(f"slot-filler trainer done: output={out_s}")


@cli.command("derive-linker-pairs")
@click.option(
    "--in",
    "in_jsonl",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Input v3 skeleton corpus (e.g. data/skeleton_train_v3_ultimate.jsonl).",
)
@click.option(
    "--out",
    "out_jsonl",
    type=click.Path(path_type=Path),
    required=True,
    help="Output Stage 1 JSONL.",
)
@click.option("--max-rows", type=int, default=None)
@click.option("--negatives-per-positive", type=int, default=3)
@click.option("--seed", type=int, default=0xCAFEF00D)
def derive_linker_pairs_cmd(
    in_jsonl: Path,
    out_jsonl: Path,
    max_rows: int | None,
    negatives_per_positive: int,
    seed: int,
) -> None:
    """Derive Stage 1 (linker) corpus from a v3 skeleton corpus.

    Each row's ranked_schema becomes positive Stage 1 records (one per
    entity / field), plus cross-row hard-negative distractors. Trains
    the linker on multi-entity disambiguation rather than the v0.2
    single-item ranking baseline.
    """
    from .generators_linker_v3 import DeriveLinkerConfig, derive_linker_pairs

    cfg = DeriveLinkerConfig(
        negatives_per_positive=negatives_per_positive, seed=seed
    )
    out_jsonl.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out_jsonl.open("w", encoding="utf-8") as fh:
        for rec in derive_linker_pairs(in_jsonl, cfg, max_rows=max_rows):
            fh.write(__import__("json").dumps(rec, sort_keys=True))
            fh.write("\n")
            n += 1
    click.echo(f"wrote {n} Stage 1 pairs to {out_jsonl}")


@cli.command("derive-slot-pairs")
@click.option(
    "--in",
    "in_jsonl",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Input v3 skeleton corpus (e.g. data/skeleton_train_v3_ultimate.jsonl).",
)
@click.option(
    "--out",
    "out_jsonl",
    type=click.Path(path_type=Path),
    required=True,
    help="Output Stage 3 JSONL.",
)
@click.option(
    "--max-rows",
    type=int,
    default=None,
    help="Cap rows produced. Default: stream whole input.",
)
@click.option("--candidates-per-slot", type=int, default=6)
@click.option("--seed", type=int, default=0xCAFEF00D)
def derive_slot_pairs_cmd(
    in_jsonl: Path,
    out_jsonl: Path,
    max_rows: int | None,
    candidates_per_slot: int,
    seed: int,
) -> None:
    """Derive Stage 3 (slot filler) corpus from a v3 skeleton corpus.

    Each row's ``slot_map`` becomes one or more Stage 3 training records
    with synthesised candidate sets including hard-negative NL stop-words
    so the cross-encoder learns to score capitalised / numeric tokens
    above grammatical fillers — Phase A's dominant failure mode.
    """
    from .generators_slot_v3 import DeriveSlotConfig, derive_slot_pairs

    cfg = DeriveSlotConfig(
        candidates_per_slot=candidates_per_slot, seed=seed
    )
    out_jsonl.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out_jsonl.open("w", encoding="utf-8") as fh:
        for rec in derive_slot_pairs(in_jsonl, cfg, max_rows=max_rows):
            fh.write(__import__("json").dumps(rec, sort_keys=True))
            fh.write("\n")
            n += 1
    click.echo(f"wrote {n} Stage 3 pairs to {out_jsonl}")


@cli.command("generate-targeted-v3")
@click.option(
    "--graph",
    "graph_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Path to a .semsql SemanticGraph file (provides FK relationships).",
)
@click.option(
    "--paraphrase-variants",
    type=int,
    default=4,
    help="NL paraphrase variants per template.",
)
@click.option(
    "--out",
    "out_jsonl",
    type=click.Path(path_type=Path),
    required=True,
    help="Output JSONL path. Appended if file exists.",
)
@click.option("--seed", type=int, default=0xCAFEF00D)
@click.option(
    "--max-join-chain",
    type=int,
    default=3,
    help="Cap on INNER-JOIN chain length (NatSQL v0.3 max = 3).",
)
@click.option(
    "--no-having",
    "include_having",
    is_flag=True,
    default=True,
    flag_value=False,
    help="Skip HAVING templates.",
)
@click.option(
    "--no-arithmetic",
    "include_arithmetic",
    is_flag=True,
    default=True,
    flag_value=False,
    help="Skip CAST / ratio templates.",
)
def generate_targeted_v3_cmd(
    graph_path: Path,
    paraphrase_variants: int,
    out_jsonl: Path,
    seed: int,
    max_join_chain: int,
    include_having: bool,
    include_arithmetic: bool,
) -> None:
    """Generate v0.3 targeted Stage-2 training pairs.

    Produces JOIN-chain (1-3 INNER JOINs over real FK edges), HAVING,
    and CAST/ratio templates that exercise the v0.3 forms which the
    Phase A BIRD smoke surfaced as primary failure buckets. See
    `docs/results/v2-bird-smoke-failures.md` for the failure histogram
    these templates target.
    """
    from .generators_targeted_v3 import (
        TargetedGeneratorConfig,
        generate_targeted_v3_pairs,
    )

    cfg = TargetedGeneratorConfig(
        paraphrase_variants=paraphrase_variants,
        seed=seed,
        max_join_chain=max_join_chain,
        include_having=include_having,
        include_arithmetic=include_arithmetic,
    )
    out_jsonl.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out_jsonl.open("a", encoding="utf-8") as fh:
        for rec in generate_targeted_v3_pairs(graph_path, cfg):
            fh.write(__import__("json").dumps(rec, sort_keys=True))
            fh.write("\n")
            n += 1
    click.echo(f"appended {n} targeted-v3 pairs to {out_jsonl}")


@cli.command("generate-pairs")
@click.option(
    "--graph",
    "graph_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Path to a .semsql SemanticGraph file.",
)
@click.option(
    "--stage",
    type=click.Choice(["skeleton", "linker", "slot-filler", "e2e"]),
    default="skeleton",
    help="Which stage's pairs to generate.",
)
@click.option(
    "--paraphrase-variants",
    type=int,
    default=4,
    help="NL paraphrase variants per template (4 recommended; 2 for faster generation).",
)
@click.option(
    "--out",
    "out_jsonl",
    type=click.Path(path_type=Path),
    required=True,
    help="Output JSONL path. Appended if the file exists.",
)
@click.option("--seed", type=int, default=0xCAFEF00D)
def generate_pairs_cmd(
    graph_path: Path,
    stage: str,
    paraphrase_variants: int,
    out_jsonl: Path,
    seed: int,
) -> None:
    """Generate synthetic training pairs from a SemanticGraph file.

    Walks all entity × field × operator × value combinations in the graph
    and emits training records in the shape each stage's trainer expects.
    Use `active-subset` after this to pick the 25K most diverse rows.

    Example (Stage 2 skeleton pairs from the finance graph):

        python -m semsql_train generate-pairs \\
            --graph target/spider_graphs/finance.semsql \\
            --stage skeleton --paraphrase-variants 2 \\
            --out data/synthetic_skeleton.jsonl
    """
    from .generators import GeneratorConfig, generate_e2e_pairs, generate_linker_pairs, generate_skeleton_pairs, generate_slot_pairs

    cfg = GeneratorConfig(paraphrase_variants=paraphrase_variants, seed=seed)
    fn_map = {
        "skeleton": generate_skeleton_pairs,
        "linker": generate_linker_pairs,
        "slot-filler": generate_slot_pairs,
        "e2e": generate_e2e_pairs,
    }
    gen_fn = fn_map[stage]

    out_jsonl.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out_jsonl.open("a", encoding="utf-8") as fh:
        for rec in gen_fn(str(graph_path), cfg):
            fh.write(__import__("json").dumps(rec, sort_keys=True))
            fh.write("\n")
            n += 1

    click.echo(f"appended {n} {stage} pairs to {out_jsonl}")


if __name__ == "__main__":
    cli()
