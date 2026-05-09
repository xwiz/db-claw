"""Stage 3 — slot filler trainer.

The slot filler is a ~5M-param classifier (cross-encoder shape, same
architecture as Stage 1 with a smaller body) trained to pick the right
candidate for every ``@entityN`` / ``@fieldN`` / ``@valN`` placeholder
in a NatSQL skeleton.

Each training record is one slot decision:

    {
      "nl":          "students with balance over 100",
      "skeleton":    "SELECT * FROM @entity1 WHERE @field1 > @val1",
      "slot_name":   "@field1",
      "candidates":  ["users.balance", "users.email", "users.id"],
      "correct_index": 0
    }

Training is multi-class softmax over the candidate set (typically <10
candidates per slot), so the classifier head is a single linear layer
on top of the shared cross-encoder backbone.

ML imports are deferred to inside :func:`train_slot_filler` so the
package stays usable without torch installed. :func:`build_dataset`
and :func:`preflight` are torch-free.

Pre-flight checks the trainer runs *before* importing torch:

- Train + dev manifests exist.
- Every record has the required keys.
- ``correct_index`` is a valid index into ``candidates``.
- ``candidates`` is non-empty and bounded (≤32 — a runaway
  candidate generator silently kills batch throughput).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "SlotFillerTrainConfig",
    "PreflightReport",
    "build_dataset",
    "preflight",
    "train_slot_filler",
    "write_jsonl",
]

_REQUIRED_KEYS = ("nl", "skeleton", "slot_name", "candidates", "correct_index")
_MAX_CANDIDATES_SANE = 32


@dataclass(frozen=True)
class SlotFillerTrainConfig:
    """Knobs for one slot-filler fine-tune run."""

    train_jsonl: Path
    eval_jsonl: Path
    output_dir: Path
    base_model: str = "distilbert-base-uncased"
    """Same teacher backbone as Stage 1 — distilled even further (~5M
    params) since the slot decision is multi-class over a tiny candidate
    set, not free-form generation."""

    epochs: int = 4
    batch_size: int = 64
    learning_rate: float = 2e-5
    seed: int = 42

    student_hidden_layers: int = 3
    student_hidden_size: int = 256

    max_seq_len: int = 128

    bf16: bool = False

    extra: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class PreflightReport:
    """Outcome of :func:`preflight`. Gates the actual training run."""

    train_count: int
    eval_count: int
    avg_candidates: float
    issues: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.issues


# ---------------------------------------------------------------------------
# offline helpers (no torch required)
# ---------------------------------------------------------------------------


def build_dataset(jsonl_path: Path) -> Iterator[dict]:
    """Stream JSONL records and validate each on the fly.

    Raises on:

    - missing required key,
    - empty / oversized candidate list,
    - ``correct_index`` outside ``range(len(candidates))``.
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
                    raise ValueError(
                        f"{jsonl_path}:{lineno}: missing required key {key!r}"
                    )
            cands = rec["candidates"]
            if not isinstance(cands, list) or not cands:
                raise ValueError(
                    f"{jsonl_path}:{lineno}: candidates must be a non-empty list"
                )
            if len(cands) > _MAX_CANDIDATES_SANE:
                raise ValueError(
                    f"{jsonl_path}:{lineno}: candidates length {len(cands)} "
                    f"exceeds sanity cap {_MAX_CANDIDATES_SANE}"
                )
            idx = rec["correct_index"]
            if not isinstance(idx, int) or idx < 0 or idx >= len(cands):
                raise ValueError(
                    f"{jsonl_path}:{lineno}: correct_index {idx!r} "
                    f"out of range for {len(cands)} candidates"
                )
            yield rec


def preflight(cfg: SlotFillerTrainConfig) -> PreflightReport:
    """Run every offline check that doesn't need torch."""
    issues: list[str] = []

    train_count = 0
    candidate_total = 0
    if cfg.train_jsonl.exists():
        for rec in build_dataset(cfg.train_jsonl):
            train_count += 1
            candidate_total += len(rec["candidates"])
    else:
        issues.append(f"train file missing: {cfg.train_jsonl}")

    eval_count = 0
    if cfg.eval_jsonl.exists():
        eval_count = sum(1 for _ in build_dataset(cfg.eval_jsonl))
    else:
        issues.append(f"eval file missing: {cfg.eval_jsonl}")

    avg_cands = (candidate_total / train_count) if train_count else 0.0
    # 1 candidate is degenerate — it makes every example trivially
    # correct and biases the loss toward zero.
    if train_count > 0 and avg_cands < 1.5:
        issues.append(
            f"average candidate-list size {avg_cands:.2f} is below 1.5 — "
            "Stage 1 should produce at least a few distractors per slot"
        )

    if cfg.epochs <= 0:
        issues.append(f"epochs={cfg.epochs} must be positive")
    if cfg.batch_size <= 0:
        issues.append(f"batch_size={cfg.batch_size} must be positive")
    if cfg.max_seq_len <= 0:
        issues.append(f"max_seq_len={cfg.max_seq_len} must be positive")

    return PreflightReport(
        train_count=train_count,
        eval_count=eval_count,
        avg_candidates=avg_cands,
        issues=tuple(issues),
    )


# ---------------------------------------------------------------------------
# trainer entry point — torch deferred to invocation
# ---------------------------------------------------------------------------


def train_slot_filler(cfg: SlotFillerTrainConfig) -> Path:
    """Run distillation + fine-tuning. Returns the output directory."""
    report = preflight(cfg)
    if not report.ok:
        raise RuntimeError(
            "slot-filler preflight failed:\n"
            + "\n".join(f"  - {x}" for x in report.issues)
        )

    try:
        # See note in trainers/skeleton.py — pandas/datasets MUST be
        # imported before torch/transformers on Python 3.13 + Windows or
        # Trainer.__init__ blows the OS stack via pandas._libs.tslibs.
        import pandas  # noqa: F401
        import datasets  # noqa: F401
        import torch  # noqa: F401
        import transformers  # noqa: F401
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "Stage 3 training requires `pip install semsql-train[ml]`."
        ) from e

    transformers.set_seed(cfg.seed)

    tokenizer = transformers.AutoTokenizer.from_pretrained(cfg.base_model)
    model = transformers.AutoModelForSequenceClassification.from_pretrained(
        cfg.base_model, num_labels=2
    )

    # Per-slot decision is multi-class softmax over candidates at inference,
    # but BCE per (slot, candidate) pair at training time keeps the loss
    # local and lets us reuse the cross-encoder shape from Stage 1.  Each
    # input record yields ``len(candidates)`` rows.
    def _flatten(records: list[dict]) -> list[dict]:
        flat: list[dict] = []
        for rec in records:
            cands = rec["candidates"]
            correct = int(rec["correct_index"])
            for i, cand in enumerate(cands):
                flat.append(
                    {
                        "nl": rec["nl"],
                        "skeleton": rec["skeleton"],
                        "slot_name": rec["slot_name"],
                        "candidate": cand,
                        "label": 1 if i == correct else 0,
                    }
                )
        return flat

    class _SlotDataset(torch.utils.data.Dataset):
        def __init__(self, recs: list[dict]) -> None:
            self.recs = recs

        def __len__(self) -> int:
            return len(self.recs)

        def __getitem__(self, idx: int) -> dict:
            rec = self.recs[idx]
            text_a = rec["nl"]
            text_b = (
                f"slot {rec['slot_name']} in [{rec['skeleton']}]: {rec['candidate']}"
            )
            enc = tokenizer(
                text_a,
                text_b,
                max_length=cfg.max_seq_len,
                truncation=True,
                padding=False,
            )
            enc["labels"] = rec["label"]
            return enc

    train_recs = _flatten(list(build_dataset(cfg.train_jsonl)))
    eval_recs = _flatten(list(build_dataset(cfg.eval_jsonl)))
    train_ds = _SlotDataset(train_recs)
    eval_ds = _SlotDataset(eval_recs)

    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    bf16 = (
        cfg.bf16 and torch.cuda.is_available() and torch.cuda.is_bf16_supported()
    )
    args = transformers.TrainingArguments(
        output_dir=str(cfg.output_dir),
        num_train_epochs=cfg.epochs,
        per_device_train_batch_size=cfg.batch_size,
        per_device_eval_batch_size=cfg.batch_size,
        learning_rate=cfg.learning_rate,
        weight_decay=0.01,
        warmup_ratio=0.05,
        lr_scheduler_type="linear",
        seed=cfg.seed,
        logging_steps=20,
        save_strategy="no",
        eval_strategy="no",
        report_to=[],
        bf16=bf16,
        dataloader_pin_memory=torch.cuda.is_available(),
    )

    collator = transformers.DataCollatorWithPadding(tokenizer=tokenizer)
    import inspect

    trainer_kwargs: dict[str, object] = dict(
        model=model,
        args=args,
        train_dataset=train_ds,
        eval_dataset=eval_ds,
        data_collator=collator,
    )
    sig = inspect.signature(transformers.Trainer.__init__)
    if "processing_class" in sig.parameters:
        trainer_kwargs["processing_class"] = tokenizer
    else:
        trainer_kwargs["tokenizer"] = tokenizer

    trainer = transformers.Trainer(**trainer_kwargs)
    trainer.train()
    trainer.save_model(str(cfg.output_dir))
    tokenizer.save_pretrained(str(cfg.output_dir))
    return cfg.output_dir


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
