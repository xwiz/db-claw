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

from .spider_linker import (
    SpiderLinkerConfig,
    generate_linker_pairs_from_spider_with_stats,
    write_pairs_jsonl,
)
from .trainers.distill import DistillConfig, distill_linker, preflight as distill_preflight
from .trainers.linker import LinkerTrainConfig, preflight as linker_preflight


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


if __name__ == "__main__":
    cli()
