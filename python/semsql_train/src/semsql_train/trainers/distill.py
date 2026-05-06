"""Stage 1 cross-encoder distillation.

Implements the published RESDSQL recipe with ergonomic defaults: a
DistilBERT-base teacher (or any HF cross-encoder) is distilled into a
4-layer / 384-hidden student via:

    L = (1 - α) · CE(student, label) + α · T² · KL(softmax(student/T) || softmax(teacher/T))

The temperature ``T`` (default 4.0) and the mixing weight ``α`` (default
0.7) come from the original Hinton paper; the values are robust across
schema-linking corpora in our pilot runs.

Why this shape, specifically:

* **CE term anchors the student to the hard labels.** Without it the
  student over-fits the teacher's softmax noise.
* **KL term transfers the teacher's confidence calibration.** The
  cross-encoder needs to rank ``users.email`` higher than
  ``orders.email`` even when both are "relevant"; the soft labels carry
  that ranking information.
* **T² scaling** is the canonical fix for the gradient-magnitude
  asymmetry between the two terms.

The student architecture is a copy of the teacher with ``num_hidden_layers``
and ``hidden_size`` overridden in the config. We initialise the student's
embedding table and first ``student_hidden_layers`` layers from the
teacher (RESDSQL's "layer dropping" strategy) — measurably faster
convergence than random init for cross-encoder distillation.

This module imports torch / transformers lazily; until they're available
the public surface (``DistillConfig`` and the accept-only-jsonl
``preflight``) is callable so CI can validate the data path without a
GPU.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "DistillConfig",
    "DistillReport",
    "preflight",
    "distill_linker",
]


@dataclass(frozen=True)
class DistillConfig:
    """Knobs for one distillation run."""

    train_jsonl: Path
    eval_jsonl: Path
    output_dir: Path
    teacher_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    """Default teacher: a small but well-calibrated cross-encoder. Swap for
    ``distilbert-base-uncased`` when training the teacher from scratch on
    Spider+BIRD (longer pre-training; outside this script's scope)."""

    # Student architecture
    student_hidden_layers: int = 4
    student_hidden_size: int = 384

    # Distillation hyperparameters (Hinton 2015 defaults, RESDSQL validated)
    temperature: float = 4.0
    alpha: float = 0.7
    """Mixing weight: ``alpha * KL + (1-alpha) * CE``."""

    epochs: int = 3
    batch_size: int = 64
    learning_rate: float = 2e-5
    warmup_ratio: float = 0.06
    weight_decay: float = 0.01
    max_seq_len: int = 128
    """Tight bound — schema items are short ("users.email"). 128 covers
    the question + item with room to spare."""

    seed: int = 42
    fp16: bool = True
    """Mixed-precision training. Off only on CPU smoke tests."""

    log_every: int = 50
    eval_every_epoch: bool = True

    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DistillReport:
    """Outcome of one distillation run."""

    student_param_count: int
    epochs_completed: int
    final_train_loss: float
    final_eval_loss: float
    eval_recall_at_5: float
    output_dir: Path


# ---------------------------------------------------------------------------
# preflight (no torch needed)
# ---------------------------------------------------------------------------


def preflight(cfg: DistillConfig) -> tuple[bool, list[str]]:
    """Sanity-check the config + data before any GPU spend."""
    issues: list[str] = []
    if not cfg.train_jsonl.exists():
        issues.append(f"train data missing: {cfg.train_jsonl}")
    if not cfg.eval_jsonl.exists():
        issues.append(f"eval data missing: {cfg.eval_jsonl}")
    if not (0.0 < cfg.alpha < 1.0):
        issues.append(f"alpha={cfg.alpha} must be in (0, 1)")
    if cfg.temperature <= 1.0:
        issues.append(
            f"temperature={cfg.temperature} should be > 1 — soft labels need spread"
        )
    if cfg.student_hidden_layers <= 0 or cfg.student_hidden_layers > 12:
        issues.append(
            f"student_hidden_layers={cfg.student_hidden_layers} out of range (1..12)"
        )
    if cfg.epochs <= 0 or cfg.batch_size <= 0:
        issues.append("epochs and batch_size must be positive")

    # Sample the first 500 records to verify shape — full validation runs
    # inside `distill_linker` once torch is imported.
    if cfg.train_jsonl.exists():
        seen = 0
        positives = 0
        with cfg.train_jsonl.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    issues.append(f"malformed JSON on line {seen + 1}")
                    break
                if "nl" not in rec or "candidate_target" not in rec:
                    issues.append(f"missing keys on line {seen + 1}")
                    break
                if rec.get("relevance_label") == 1.0:
                    positives += 1
                seen += 1
                if seen >= 500:
                    break
        if seen and positives / seen < 0.05:
            issues.append(
                f"positive fraction {positives / seen:.1%} (in first {seen} rows) "
                "is below 5% — check the negative ratio in the generator"
            )

    return (not issues, issues)


# ---------------------------------------------------------------------------
# distillation entry point — torch deferred
# ---------------------------------------------------------------------------


def distill_linker(cfg: DistillConfig) -> DistillReport:
    """Run teacher → student distillation. Returns the final metrics.

    Heavy ML imports are local so the rest of the package stays usable
    without a torch install. If the imports fail, the caller gets a
    helpful pointer to the ``[ml]`` extra rather than a stack trace.
    """
    ok, issues = preflight(cfg)
    if not ok:
        raise RuntimeError("distill preflight failed:\n" + "\n".join(f"  - {x}" for x in issues))

    try:
        import torch  # noqa: F401
        import torch.nn.functional as F  # noqa: N812
        from torch.optim import AdamW
        from torch.utils.data import DataLoader, Dataset
        from transformers import (
            AutoConfig,
            AutoModelForSequenceClassification,
            AutoTokenizer,
            get_linear_schedule_with_warmup,
        )
    except ImportError as e:  # pragma: no cover — exercised only without ML extras
        raise RuntimeError(
            "Stage 1 distillation requires `pip install semsql-train[ml]`."
        ) from e

    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    torch.manual_seed(cfg.seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(cfg.seed)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    tokenizer = AutoTokenizer.from_pretrained(cfg.teacher_model)

    # ---- teacher (frozen) ----
    # Force a 2-class head. Many cross-encoder checkpoints on the Hub
    # ship `num_labels=1` (regression) which is incompatible with the
    # KL-div + CE loss below. `ignore_mismatched_sizes=True` lets HF
    # reinitialise the head shape — the user is expected to fine-tune
    # the teacher on their own (relevance, not-relevance) corpus before
    # distilling, OR pass a teacher_model that already has a 2-class
    # head. We document this in the click CLI help.
    teacher = AutoModelForSequenceClassification.from_pretrained(
        cfg.teacher_model,
        num_labels=2,
        ignore_mismatched_sizes=True,
    )
    teacher.eval()
    for p in teacher.parameters():
        p.requires_grad = False
    teacher.to(device)

    # ---- student (truncated copy of teacher; layers re-initialised below) ----
    student_config = AutoConfig.from_pretrained(cfg.teacher_model)
    student_config.num_hidden_layers = cfg.student_hidden_layers
    student_config.hidden_size = _round_to_head_multiple(
        cfg.student_hidden_size, student_config.num_attention_heads
    )
    # Mirror the teacher's label cardinality. Anything else makes the
    # KL-div shape-mismatch.
    student_config.num_labels = teacher.config.num_labels
    student = AutoModelForSequenceClassification.from_config(student_config)
    _copy_lower_layers_(teacher, student, n_layers=cfg.student_hidden_layers)
    student.to(device)

    student_param_count = sum(p.numel() for p in student.parameters())

    # ---- data ----
    class JsonlPairDataset(Dataset):
        def __init__(self, path: Path) -> None:
            self.records = list(_iter_jsonl(path))

        def __len__(self) -> int:
            return len(self.records)

        def __getitem__(self, idx: int) -> dict[str, Any]:
            r = self.records[idx]
            return {
                "nl": r["nl"],
                "schema_item": r["candidate_target"],
                "label": float(r["relevance_label"]),
            }

    def collate(batch: list[dict]) -> dict[str, Any]:
        enc = tokenizer(
            [b["nl"] for b in batch],
            [b["schema_item"] for b in batch],
            padding=True,
            truncation=True,
            max_length=cfg.max_seq_len,
            return_tensors="pt",
        )
        # Threshold at 0.5 instead of `int(...)` truncation. The current
        # Spider-linker generator emits hard 0.0/1.0 labels but soft
        # labels (e.g. teacher-distilled relevance scores) are a
        # forward-compatible feature — `int(0.7)` would silently demote
        # them to 0.
        bin_labels = [1 if float(b["label"]) >= 0.5 else 0 for b in batch]
        labels = torch.tensor(bin_labels, dtype=torch.long, device=device)
        out: dict[str, Any] = {k: v.to(device) for k, v in enc.items()}
        out["labels"] = labels
        # Thread the raw question + binarised label through the batch so
        # the eval pass can group by question identity (not by tokenised
        # bytes — different padding across batches breaks that).
        out["_nl"] = [b["nl"] for b in batch]
        out["_label_raw"] = bin_labels
        return out

    train_ds = JsonlPairDataset(cfg.train_jsonl)
    eval_ds = JsonlPairDataset(cfg.eval_jsonl)
    train_loader = DataLoader(
        train_ds, batch_size=cfg.batch_size, shuffle=True, collate_fn=collate
    )
    eval_loader = DataLoader(
        eval_ds, batch_size=cfg.batch_size, shuffle=False, collate_fn=collate
    )

    # ---- optimiser + schedule ----
    total_steps = cfg.epochs * max(1, len(train_loader))
    optim = AdamW(student.parameters(), lr=cfg.learning_rate, weight_decay=cfg.weight_decay)
    sched = get_linear_schedule_with_warmup(
        optim,
        num_warmup_steps=int(cfg.warmup_ratio * total_steps),
        num_training_steps=total_steps,
    )
    use_amp = cfg.fp16 and device.type == "cuda"
    scaler = torch.amp.GradScaler("cuda", enabled=use_amp)

    # ---- train loop ----
    final_train_loss = math.nan
    epochs_completed = 0
    for epoch in range(cfg.epochs):
        student.train()
        epoch_loss_sum = 0.0
        epoch_steps = 0
        running = 0.0
        for step, batch in enumerate(train_loader):
            optim.zero_grad(set_to_none=True)
            with torch.amp.autocast("cuda", enabled=use_amp):
                with torch.no_grad():
                    teacher_logits = teacher(
                        input_ids=batch["input_ids"],
                        attention_mask=batch["attention_mask"],
                    ).logits
                student_out = student(
                    input_ids=batch["input_ids"],
                    attention_mask=batch["attention_mask"],
                ).logits
                ce = F.cross_entropy(student_out, batch["labels"])
                kl = F.kl_div(
                    F.log_softmax(student_out / cfg.temperature, dim=-1),
                    F.softmax(teacher_logits / cfg.temperature, dim=-1),
                    reduction="batchmean",
                ) * (cfg.temperature ** 2)
                loss = (1 - cfg.alpha) * ce + cfg.alpha * kl

            scaler.scale(loss).backward()
            scaler.unscale_(optim)
            torch.nn.utils.clip_grad_norm_(student.parameters(), max_norm=1.0)
            scaler.step(optim)
            scaler.update()
            sched.step()

            step_loss = float(loss.item())
            epoch_loss_sum += step_loss
            epoch_steps += 1
            running += step_loss
            if (step + 1) % cfg.log_every == 0:
                print(
                    f"epoch={epoch + 1} step={step + 1}/{len(train_loader)} "
                    f"loss={running / cfg.log_every:.4f} "
                    f"lr={sched.get_last_lr()[0]:.2e}",
                    flush=True,
                )
                running = 0.0

        epochs_completed = epoch + 1
        # Average loss across the whole epoch — robust to log_every >
        # batch_count and to non-multiples.
        final_train_loss = epoch_loss_sum / max(1, epoch_steps)

        if cfg.eval_every_epoch:
            eval_loss, recall = _eval_pass(
                student, teacher, eval_loader, cfg, device, F
            )
            print(
                f"epoch={epoch + 1} eval_loss={eval_loss:.4f} recall@5={recall:.3f}",
                flush=True,
            )

    # ---- final eval + checkpoint ----
    eval_loss, recall = _eval_pass(student, teacher, eval_loader, cfg, device, F)
    student.save_pretrained(cfg.output_dir)
    tokenizer.save_pretrained(cfg.output_dir)
    (cfg.output_dir / "distill_config.json").write_text(
        json.dumps(_distill_config_dict(cfg), indent=2, sort_keys=True),
        encoding="utf-8",
    )

    return DistillReport(
        student_param_count=student_param_count,
        epochs_completed=epochs_completed,
        final_train_loss=float(final_train_loss),
        final_eval_loss=float(eval_loss),
        eval_recall_at_5=float(recall),
        output_dir=cfg.output_dir,
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _round_to_head_multiple(hidden: int, heads: int) -> int:
    """Hidden size must be divisible by ``num_attention_heads``."""
    return max(heads, (hidden // heads) * heads)


def _copy_lower_layers_(teacher: Any, student: Any, *, n_layers: int) -> None:
    """Initialise the student's transformer stack from the teacher's
    bottom ``n_layers``. Embedding + classifier heads are shared too.

    No-op (silently) if the architectures don't expose a compatible
    structure — the student then trains from random init, which is
    slower but still converges.
    """
    try:
        s_enc = student.distilbert.transformer  # DistilBERT-class
        t_enc = teacher.distilbert.transformer
        for i in range(min(n_layers, len(t_enc.layer))):
            s_enc.layer[i].load_state_dict(t_enc.layer[i].state_dict(), strict=False)
        student.distilbert.embeddings.load_state_dict(
            teacher.distilbert.embeddings.state_dict(), strict=False
        )
    except AttributeError:
        try:
            s_enc = student.bert.encoder  # BERT-class
            t_enc = teacher.bert.encoder
            for i in range(min(n_layers, len(t_enc.layer))):
                s_enc.layer[i].load_state_dict(t_enc.layer[i].state_dict(), strict=False)
            student.bert.embeddings.load_state_dict(
                teacher.bert.embeddings.state_dict(), strict=False
            )
        except AttributeError:
            return  # unknown architecture — train from random init


def _iter_jsonl(path: Path) -> Iterator[dict]:
    with Path(path).open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def _eval_pass(
    student: Any,
    teacher: Any,
    loader: Any,
    cfg: DistillConfig,
    device: Any,
    F: Any,
) -> tuple[float, float]:
    """Return ``(eval_loss, recall_at_5)``.

    Recall@5 is computed per-question: every batch row is bucketed by
    its raw NL string (threaded through ``collate`` as ``_nl``), then
    ranked by the student's class-1 softmax probability. Recall is the
    fraction of gold (``label == 1``) candidates that appear in the
    top 5.

    Score-based de-dup of the gold set is wrong (different candidates
    can land at identical scores); we instead key by candidate identity
    via the row index inside the question's bucket.
    """
    import torch as _torch

    student.eval()
    total_loss = 0.0
    total_batches = 0
    # nl -> [(score, label, candidate_id), ...]
    grouped: dict[str, list[tuple[float, int, int]]] = {}
    counter = 0
    with _torch.no_grad():
        for batch in loader:
            student_out = student(
                input_ids=batch["input_ids"],
                attention_mask=batch["attention_mask"],
            ).logits
            teacher_logits = teacher(
                input_ids=batch["input_ids"],
                attention_mask=batch["attention_mask"],
            ).logits
            ce = F.cross_entropy(student_out, batch["labels"])
            kl = F.kl_div(
                F.log_softmax(student_out / cfg.temperature, dim=-1),
                F.softmax(teacher_logits / cfg.temperature, dim=-1),
                reduction="batchmean",
            ) * (cfg.temperature ** 2)
            loss = (1 - cfg.alpha) * ce + cfg.alpha * kl
            total_loss += float(loss.item())
            total_batches += 1
            scores = student_out.softmax(dim=-1)[:, 1].detach().cpu().tolist()
            for nl, score, label_raw in zip(
                batch["_nl"], scores, batch["_label_raw"], strict=True
            ):
                grouped.setdefault(nl, []).append((float(score), int(label_raw), counter))
                counter += 1

    recalls: list[float] = []
    for items in grouped.values():
        gold_ids = {cid for _s, lb, cid in items if lb == 1}
        if not gold_ids:
            continue
        ranked = sorted(items, key=lambda it: -it[0])[:5]
        hit = sum(1 for _s, _lb, cid in ranked if cid in gold_ids)
        recalls.append(hit / len(gold_ids))
    recall_at_5 = (sum(recalls) / len(recalls)) if recalls else 0.0

    return (total_loss / max(1, total_batches), recall_at_5)


def _distill_config_dict(cfg: DistillConfig) -> dict[str, Any]:
    """Pickle-free snapshot of the config for the run directory."""
    return {
        "teacher_model": cfg.teacher_model,
        "student_hidden_layers": cfg.student_hidden_layers,
        "student_hidden_size": cfg.student_hidden_size,
        "temperature": cfg.temperature,
        "alpha": cfg.alpha,
        "epochs": cfg.epochs,
        "batch_size": cfg.batch_size,
        "learning_rate": cfg.learning_rate,
        "warmup_ratio": cfg.warmup_ratio,
        "weight_decay": cfg.weight_decay,
        "max_seq_len": cfg.max_seq_len,
        "seed": cfg.seed,
        "fp16": cfg.fp16,
        "host": os.uname().nodename if hasattr(os, "uname") else os.environ.get("COMPUTERNAME", "unknown"),
    }
