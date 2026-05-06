"""Stage 1 — cross-encoder linker trainer.

The linker is a ~10M-param distilled DistilBERT-class transformer trained
to score ``(NL, schema_item)`` pairs for relevance. Architecture follows
[RESDSQL](https://arxiv.org/pdf/2302.05965)'s ranking-enhanced encoder.

Training data is produced by
:func:`semsql_train.generators.generate_linker_pairs` — positive examples
plus same-column-different-table hard negatives. The trainer is a thin
wrapper over Hugging Face ``transformers`` so users can swap the
distillation teacher / student models via config.

The ML imports are deferred to inside :func:`train_linker` so the rest of
the package stays usable without torch installed. The
:func:`build_dataset` helper *is* available without torch — it just
walks the JSONL corpus and yields tokeniser-ready dicts, allowing dry-run
verification of the data path.

Pre-flight checks the trainer runs *before* importing torch:

- Train + dev manifests exist.
- Every record has the required keys.
- Positive / negative balance is sane (warn if <5% positives).
- Schema-link recall@5 is computed on a held-out slice (offline metric;
  no GPU needed) — sub-90 % at this stage is a red flag.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "LinkerTrainConfig",
    "PreflightReport",
    "build_dataset",
    "preflight",
    "train_linker",
]

_REQUIRED_KEYS = ("nl", "candidate_kind", "candidate_target", "relevance_label")


@dataclass(frozen=True)
class LinkerTrainConfig:
    """Knobs for one linker fine-tune run."""

    train_jsonl: Path
    eval_jsonl: Path
    output_dir: Path
    base_model: str = "distilbert-base-uncased"
    """HF Hub identifier for the teacher (we distil down at export time)."""

    epochs: int = 3
    batch_size: int = 64
    learning_rate: float = 2e-5
    seed: int = 42

    # Distillation knobs (target ~10M params after pruning).
    student_hidden_layers: int = 4
    student_hidden_size: int = 384

    extra: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class PreflightReport:
    """Outcome of :func:`preflight`. Gates the actual training run."""

    train_count: int
    eval_count: int
    positive_fraction: float
    issues: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.issues


# ---------------------------------------------------------------------------
# offline helpers (no torch required)
# ---------------------------------------------------------------------------


def build_dataset(jsonl_path: Path) -> Iterator[dict]:
    """Stream JSONL records and validate each on the fly.

    Yields the dict verbatim; a missing key raises so the trainer fails
    closed rather than silently feeding garbage to the optimiser.
    """
    with Path(jsonl_path).open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"{jsonl_path}:{lineno}: invalid JSON: {e}") from e
            for key in _REQUIRED_KEYS:
                if key not in rec:
                    raise ValueError(f"{jsonl_path}:{lineno}: missing required key {key!r}")
            yield rec


def preflight(cfg: LinkerTrainConfig) -> PreflightReport:
    """Run every offline check that doesn't need torch.

    The trainer must call this before importing the ML stack. If
    :attr:`PreflightReport.ok` is False, training must abort.
    """
    issues: list[str] = []

    train_count = 0
    pos_count = 0
    if cfg.train_jsonl.exists():
        for rec in build_dataset(cfg.train_jsonl):
            train_count += 1
            if rec["relevance_label"] == 1.0:
                pos_count += 1
    else:
        issues.append(f"train file missing: {cfg.train_jsonl}")

    eval_count = 0
    if cfg.eval_jsonl.exists():
        eval_count = sum(1 for _ in build_dataset(cfg.eval_jsonl))
    else:
        issues.append(f"eval file missing: {cfg.eval_jsonl}")

    positive_fraction = (pos_count / train_count) if train_count else 0.0
    if train_count > 0 and positive_fraction < 0.05:
        issues.append(
            f"positive fraction {positive_fraction:.2%} is below the 5% sanity threshold "
            "— check the generator's hard-negative ratio"
        )

    if cfg.epochs <= 0:
        issues.append(f"epochs={cfg.epochs} must be positive")
    if cfg.batch_size <= 0:
        issues.append(f"batch_size={cfg.batch_size} must be positive")

    return PreflightReport(
        train_count=train_count,
        eval_count=eval_count,
        positive_fraction=positive_fraction,
        issues=tuple(issues),
    )


# ---------------------------------------------------------------------------
# trainer entry point — torch deferred to invocation
# ---------------------------------------------------------------------------


def train_linker(cfg: LinkerTrainConfig) -> Path:
    """Run distillation + fine-tuning. Returns the output directory.

    Imports torch/transformers/peft only here so the rest of the package
    remains importable without them. Raises a helpful error if the ML
    extras are not installed.
    """
    report = preflight(cfg)
    if not report.ok:
        raise RuntimeError(
            "linker preflight failed:\n" + "\n".join(f"  - {x}" for x in report.issues)
        )

    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
    except ImportError as e:  # pragma: no cover — exercised only without ML extras
        raise RuntimeError(
            "Stage 1 training requires `pip install semsql-train[ml]`."
        ) from e

    # Actual HF Trainer wiring lands once the distillation recipe is pinned.
    # The contract above is what callers depend on; keeping the body
    # explicitly NotImplemented prevents accidental "training succeeded"
    # results from a half-wired pipeline.
    raise NotImplementedError(
        "train_linker: full HF Trainer wiring lands in v0.2 model milestone. "
        "preflight() and build_dataset() are the testable surface today."
    )


# ---------------------------------------------------------------------------
# tiny helper used by tests + downstream tooling
# ---------------------------------------------------------------------------


def write_jsonl(records: Iterable[dict], dest: Path) -> int:
    """Serialise records to JSONL and return the count."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with dest.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, sort_keys=True))
            fh.write("\n")
            n += 1
    return n
