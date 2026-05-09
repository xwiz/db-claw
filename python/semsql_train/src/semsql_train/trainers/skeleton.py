"""Stage 2 — skeleton generator trainer.

The skeleton generator is a ~20M-param distilled encoder-decoder
(``T5-mini``-class) trained to map ``(NL, ranked_schema)`` pairs onto
``natsql_skeleton`` strings with ``@entityN`` / ``@fieldN`` / ``@valN``
placeholders. Architecture mirrors the seq2seq half of the
[RESDSQL](https://arxiv.org/pdf/2302.05965) cascade; output is
constrained at decode time by llguidance against the per-query NatSQL
grammar (``crates/semsql-runtime::grammar``).

Training data is produced by
:func:`semsql_train.generators.generate_skeleton_pairs` — one record per
``(paraphrased NL, ranked schema slice, gold skeleton, gold slot map)``.

ML imports are deferred to inside :func:`train_skeleton` so the package
stays usable without torch installed. :func:`build_dataset` and
:func:`preflight` are torch-free so the data path is exercised in CI
without GPU.

Pre-flight checks the trainer runs *before* importing torch:

- Train + dev manifests exist.
- Every record has the required keys (``nl``, ``natsql_skeleton``,
  ``ranked_schema``, ``slot_map``).
- ``ranked_schema`` is a non-empty list — the linker should always have
  surfaced at least the gold entity.
- ``slot_map`` is an object (the generator's canonical map). Keys that
  don't appear in the skeleton are tolerated — the generator records the
  template's logical slots even when paraphrase or template-collapse
  removes the placeholder from the rendered skeleton string. The cascade
  ranker only looks up slots that *are* in the skeleton, so the extras
  are harmless at train time.
- Approximate skeleton length is sane (≤80 tokens by whitespace) — a
  runaway template generator would silently bloat training cost.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "SkeletonTrainConfig",
    "DistillationConfig",
    "PreflightReport",
    "build_dataset",
    "preflight",
    "train_skeleton",
    "write_jsonl",
]

_REQUIRED_KEYS = ("nl", "natsql_skeleton", "ranked_schema", "slot_map")


@dataclass(frozen=True)
class DistillationConfig:
    """Knobs for sequence-level + token-level KD (M3, `docs/stage2.md` §4.1-4.2).

    Loss formulation::

        loss = α · CE(student, teacher_outputs)        # sequence-level KD
             + β · KL(student_logits || teacher_logits)  # token-level KD
             + γ · CE(student, gold_skeleton)          # gold supervision

    Defaults match `docs/stage2.md` §4.2 (small grid-search-tuned).
    """

    teacher_model: str
    """HF identifier of the teacher (e.g. ``google/t5-efficient-base``)."""

    alpha: float = 0.5
    """Weight on sequence-level KD (CE against teacher one-best outputs)."""

    beta: float = 0.3
    """Weight on token-level KD (KL divergence between teacher / student)."""

    gamma: float = 0.2
    """Weight on gold supervision."""

    temperature: float = 2.0
    """Softening temperature for the KL divergence. Higher = softer
    teacher distribution. 2.0 is the DistilBERT-paper default."""


@dataclass(frozen=True)
class SkeletonTrainConfig:
    """Knobs for one skeleton fine-tune run."""

    train_jsonl: Path
    eval_jsonl: Path
    output_dir: Path
    base_model: str = "t5-small"
    """HF Hub identifier for the seq2seq teacher (we distil + quantise at
    export time). ``t5-small`` is the M1 default — widely cached, runs
    on CPU. Production cuts upgrade to ``google/t5-efficient-base`` per
    `docs/stage2.md` §2.1."""

    epochs: int = 5
    batch_size: int = 32
    learning_rate: float = 3e-4
    seed: int = 42

    # Distillation knobs — target ~20M params after pruning + int8 quant.
    # The "scale" preset bumps these for the v1.0 ~50M-param variant per
    # `docs/stage2.md` §10 / Plan §4.4 (escalation tier).
    student_encoder_layers: int = 4
    student_decoder_layers: int = 4
    student_d_model: int = 384

    @staticmethod
    def scaled_up(
        train_jsonl: Path,
        eval_jsonl: Path,
        output_dir: Path,
        **overrides: object,
    ) -> "SkeletonTrainConfig":
        """v1.0 ~50M-param config (`docs/stage2.md` §10, Plan §10).

        Bumps encoder / decoder depth to 6 and `d_model` to 512 — the
        "scaled-up Stage 2" path is the optional v1.0 config that trades
        a few MB and a few ms of latency for ~3-5 % skeleton-match lift
        on Spider. Caller can override any field via kwargs.
        """
        defaults = dict(
            train_jsonl=train_jsonl,
            eval_jsonl=eval_jsonl,
            output_dir=output_dir,
            base_model="google/t5-efficient-base",
            student_encoder_layers=6,
            student_decoder_layers=6,
            student_d_model=512,
            epochs=5,
            batch_size=16,
            learning_rate=2e-4,
        )
        defaults.update(overrides)
        return SkeletonTrainConfig(**defaults)  # type: ignore[arg-type]

    max_source_tokens: int = 256
    max_target_tokens: int = 96

    # Test / smoke knobs. ``max_steps`` caps the optimiser steps for
    # CI smoke runs (overrides ``epochs`` when set). ``gradient_accum``
    # mirrors the batch-size×accum effective batch from `docs/stage2.md`
    # §4.3 — kept tweakable so users with smaller GPUs can dial it up.
    max_steps: int | None = None
    gradient_accum: int = 1

    # M3 distillation. ``None`` → straight gold supervision (M1 default).
    # When set, the trainer loads ``teacher_model`` alongside the student
    # and adds the seq-level + token-level KD components to the loss.
    distillation: DistillationConfig | None = None

    # ── 2026 laptop accelerators (`docs/training-on-laptop.md` §3) ─────
    # Each is a one-flag toggle. Defaults are False so M1 / CI runs stay
    # dependency-light; explicit opt-in is required.
    bf16: bool = False
    """Mixed-precision bf16. Halves memory + ~2× throughput on Ada (RTX
    4060+) / Hopper / TPU. Off by default so CPU CI doesn't trip on the
    half-tensor codepath."""

    flash_attention: str | None = None
    """One of ``"flash_attention_2"``, ``"sdpa"``, ``None``. Forwarded
    verbatim to ``AutoModelForSeq2SeqLM.from_pretrained(attn_implementation=...)``.
    FA-2 needs Ada/Hopper + Linux/WSL or Windows-CUDA wheel; ``sdpa``
    is a portable fallback that uses PyTorch's fused SDPA."""

    torch_compile: bool = False
    """Wrap the student with ``torch.compile`` (inductor backend).
    Trades ~1.5-2× throughput for a ~30 sec compile-cache warm-up on the
    first step. Skip on tiny CI smoke runs where compile cost dominates."""

    liger_kernel: bool = False
    """Apply linkedin/Liger-Kernel monkey-patch to the HF model — fused
    attention/RMSNorm/Glu kernels that drop ~25% memory and add ~15%
    throughput on T5-class students. Requires ``pip install liger-kernel``;
    silently no-ops if the package is missing."""

    extra: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class PreflightReport:
    """Outcome of :func:`preflight`. Gates the actual training run."""

    train_count: int
    eval_count: int
    avg_skeleton_len: float
    issues: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.issues


# ---------------------------------------------------------------------------
# offline helpers (no torch required)
# ---------------------------------------------------------------------------


def build_dataset(jsonl_path: Path) -> Iterator[dict]:
    """Stream JSONL records and validate each on the fly.

    Yields the dict verbatim; a missing key, malformed slot map, or empty
    schema slice raises so the trainer fails closed rather than feeding
    silently-bad data to the optimiser.
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
            if not isinstance(rec["ranked_schema"], list) or not rec["ranked_schema"]:
                raise ValueError(
                    f"{jsonl_path}:{lineno}: ranked_schema must be a non-empty list"
                )
            slot_map = rec["slot_map"]
            if not isinstance(slot_map, dict):
                raise ValueError(
                    f"{jsonl_path}:{lineno}: slot_map must be an object"
                )
            yield rec


def preflight(cfg: SkeletonTrainConfig) -> PreflightReport:
    """Run every offline check that doesn't need torch.

    The trainer must call this before importing the ML stack. If
    :attr:`PreflightReport.ok` is False, training must abort.
    """
    issues: list[str] = []

    train_count = 0
    skeleton_token_total = 0
    if cfg.train_jsonl.exists():
        for rec in build_dataset(cfg.train_jsonl):
            train_count += 1
            skeleton_token_total += len(rec["natsql_skeleton"].split())
    else:
        issues.append(f"train file missing: {cfg.train_jsonl}")

    eval_count = 0
    if cfg.eval_jsonl.exists():
        eval_count = sum(1 for _ in build_dataset(cfg.eval_jsonl))
    else:
        issues.append(f"eval file missing: {cfg.eval_jsonl}")

    avg_len = (skeleton_token_total / train_count) if train_count else 0.0
    if train_count > 0 and avg_len > 80.0:
        issues.append(
            f"average skeleton length {avg_len:.1f} tokens exceeds the 80-token "
            "sanity threshold — check the template generator"
        )

    if cfg.epochs <= 0:
        issues.append(f"epochs={cfg.epochs} must be positive")
    if cfg.batch_size <= 0:
        issues.append(f"batch_size={cfg.batch_size} must be positive")
    if cfg.max_target_tokens <= 0:
        issues.append(
            f"max_target_tokens={cfg.max_target_tokens} must be positive"
        )

    return PreflightReport(
        train_count=train_count,
        eval_count=eval_count,
        avg_skeleton_len=avg_len,
        issues=tuple(issues),
    )


# ---------------------------------------------------------------------------
# trainer entry point — torch deferred to invocation
# ---------------------------------------------------------------------------


def train_skeleton(cfg: SkeletonTrainConfig) -> Path:
    """Run skeleton-generator fine-tuning. Returns the output directory.

    Implements milestone M1 from `docs/stage2.md`: train_skeleton runs
    end-to-end on the synthetic mini-corpus. Distillation (sequence-level
    + token-level KD) lands in M3 — this M1 cut is straight supervised
    fine-tuning on the gold (source, skeleton) pairs.

    Imports torch / transformers / datasets only inside this function so
    the rest of the package remains importable without them. Raises a
    helpful error if the ML extras are not installed.
    """
    report = preflight(cfg)
    if not report.ok:
        raise RuntimeError(
            "skeleton preflight failed:\n"
            + "\n".join(f"  - {x}" for x in report.issues)
        )

    try:
        # pandas / datasets must be imported BEFORE torch/transformers on
        # Python 3.13 + Windows.  The pandas._libs.tslibs init chain is
        # deeply recursive; if it runs *inside* the already-deep
        # torch/transformers import stack it overflows the OS call stack
        # (manifesting as exit-139 / SIGSEGV).  Importing them first,
        # while the call stack is still shallow, loads all Cython
        # extensions cleanly and populates sys.modules so the later
        # `import datasets` inside Trainer.__init__ is a no-op lookup.
        import pandas  # noqa: F401
        import datasets  # noqa: F401
        import torch
        import transformers
    except ImportError as e:  # pragma: no cover — exercised only without ML extras
        raise RuntimeError(
            "Stage 2 training requires `pip install semsql-train[ml]`."
        ) from e

    transformers.set_seed(cfg.seed)

    tokenizer = transformers.AutoTokenizer.from_pretrained(cfg.base_model)

    # Forward attn_implementation when the user opted in. HF accepts
    # ``"flash_attention_2"`` or ``"sdpa"``; passing None keeps the model's
    # default (`"eager"` for older releases, `"sdpa"` for newer).
    model_kwargs: dict[str, object] = {}
    if cfg.flash_attention is not None:
        model_kwargs["attn_implementation"] = cfg.flash_attention
    if cfg.bf16 and torch.cuda.is_available():
        # Load in bf16 directly — saves the cast at first forward and
        # avoids the brief fp32 spike in VRAM.
        model_kwargs["torch_dtype"] = torch.bfloat16

    model = transformers.AutoModelForSeq2SeqLM.from_pretrained(
        cfg.base_model, **model_kwargs
    )

    # Liger Kernel — Linkedin's fused-kernel monkey-patch. Drop-in: pull
    # in the patcher and let it install its replacements on the model
    # class. We catch ImportError so the trainer keeps working when the
    # package isn't installed (CI on a torch-only env).
    if cfg.liger_kernel:
        try:
            import liger_kernel.transformers as liger  # type: ignore[import-not-found]

            # Liger exposes per-architecture patchers; pick by model class.
            patcher = getattr(liger, "apply_liger_kernel_to_t5", None)
            if patcher is not None:
                patcher(model=model)
            else:
                # Fall back to the generic patcher if the T5-specific one
                # isn't shipped in the installed Liger release.
                generic = getattr(liger, "apply_liger_kernel", None)
                if generic is not None:
                    generic(model=model)
        except ImportError:
            # Liger absent — no-op. The recipe doc tells users to expect
            # ~15% throughput loss without it; nothing else changes.
            pass

    if cfg.torch_compile:
        # `mode="reduce-overhead"` is the right default for tiny models —
        # `max-autotune` spends compile time on kernel selection that
        # only pays back on long-running batches > a few hundred steps.
        model = torch.compile(model, mode="reduce-overhead", fullgraph=False)

    # M3: optional teacher for distillation. Loaded once, frozen, kept
    # in eval mode. Tokenizer is shared with the student — every M3
    # config we ship pairs students and teachers from the same family
    # (t5-small ↔ t5-efficient-base) so the vocabularies match.
    teacher_model = None
    if cfg.distillation is not None:
        teacher_model = transformers.AutoModelForSeq2SeqLM.from_pretrained(
            cfg.distillation.teacher_model
        )
        teacher_model.eval()
        for p in teacher_model.parameters():
            p.requires_grad = False
        if torch.cuda.is_available():
            teacher_model = teacher_model.to("cuda")

    train_examples = list(build_dataset(cfg.train_jsonl))
    eval_examples = list(build_dataset(cfg.eval_jsonl))

    train_ds = _SkeletonDataset(
        train_examples, tokenizer, cfg.max_source_tokens, cfg.max_target_tokens
    )
    eval_ds = _SkeletonDataset(
        eval_examples, tokenizer, cfg.max_source_tokens, cfg.max_target_tokens
    )

    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    args_kwargs: dict[str, object] = dict(
        output_dir=str(cfg.output_dir),
        num_train_epochs=cfg.epochs,
        per_device_train_batch_size=cfg.batch_size,
        per_device_eval_batch_size=cfg.batch_size,
        gradient_accumulation_steps=cfg.gradient_accum,
        learning_rate=cfg.learning_rate,
        weight_decay=0.01,
        warmup_ratio=0.02,
        lr_scheduler_type="cosine",
        seed=cfg.seed,
        logging_steps=10,
        save_strategy="no",
        report_to=[],
        # Skip eval at end-of-step time during M1 — eval correctness
        # lands in `python/semsql_eval/per_stage.py::skeleton_exact`,
        # not inside the trainer loop. This keeps M1 compute-bound by
        # the train forward pass, not the eval generation step.
        eval_strategy="no",
        # bf16 when explicitly opted in AND the host supports it. CI on
        # CPU never has both, so the trainer falls back to fp32.
        bf16=cfg.bf16
        and torch.cuda.is_available()
        and torch.cuda.is_bf16_supported(),
        # Suppress the pin-memory warning when running on CPU; HF
        # defaults this on, which is harmless but emits a warning that
        # CI's `-W error` policy treats as a failure.
        dataloader_pin_memory=torch.cuda.is_available(),
    )
    if cfg.max_steps is not None:
        args_kwargs["max_steps"] = cfg.max_steps
    args = transformers.Seq2SeqTrainingArguments(**args_kwargs)

    collator = transformers.DataCollatorForSeq2Seq(
        tokenizer=tokenizer, model=model, padding="longest"
    )
    # Newer transformers (≥4.46) prefer `processing_class`; older
    # versions only accept `tokenizer`. Probe the signature so we
    # support both without triggering deprecation warnings on either.
    import inspect

    trainer_kwargs: dict[str, object] = dict(
        model=model,
        args=args,
        train_dataset=train_ds,
        eval_dataset=eval_ds,
        data_collator=collator,
    )
    sig = inspect.signature(transformers.Seq2SeqTrainer.__init__)
    if "processing_class" in sig.parameters:
        trainer_kwargs["processing_class"] = tokenizer
    else:
        trainer_kwargs["tokenizer"] = tokenizer

    # When distillation is configured, swap in the KD-aware Trainer
    # subclass. The subclass overrides `compute_loss` to add the
    # sequence-level + token-level KD components on top of the standard
    # cross-entropy loss; without `distillation` set, we use the stock
    # Seq2SeqTrainer (M1 path) so existing tests stay green.
    if cfg.distillation is not None and teacher_model is not None:
        trainer = _DistillationTrainer(
            teacher_model=teacher_model,
            distillation=cfg.distillation,
            **trainer_kwargs,
        )
    else:
        trainer = transformers.Seq2SeqTrainer(**trainer_kwargs)

    trainer.train()
    trainer.save_model(str(cfg.output_dir))
    tokenizer.save_pretrained(str(cfg.output_dir))
    return cfg.output_dir


# ---------------------------------------------------------------------------
# M3 — distillation-aware Seq2SeqTrainer subclass.
#
# Two KD signals on top of gold supervision:
#
#   1. Sequence-level: the teacher's one-best output is the supervision
#      target. The base `Seq2SeqTrainer.compute_loss` already trains the
#      student against `inputs["labels"]`; the data-collation path
#      replaces those labels with teacher outputs when seq-KD is active.
#      For minimal complexity, this implementation uses the gold labels
#      as-is for the CE term and treats the seq-KD weight α as an
#      additional weight on the CE loss (matching what the Sanh DistilBERT
#      paper does when teacher and gold are both available — ε=0.5
#      blending acts on the *same* CE term). When the user wants strict
#      seq-level KD on teacher one-best, they pre-generate the teacher
#      outputs offline and write them as `natsql_skeleton` in the JSONL.
#
#   2. Token-level: KL(student_logits || teacher_logits) at temperature τ.
#      Computed inside `compute_loss` by running the teacher in-line on
#      the same encoder inputs, with no_grad. This is the dominant
#      compute cost in distillation training; it's batched per-step.
#
# Defined at module scope so HF's DataLoader workers can pickle it.
# ---------------------------------------------------------------------------


def _build_distillation_trainer_class():
    """Construct the trainer subclass lazily so the import only happens
    inside :func:`train_skeleton`'s code path. We can't define the class
    at module scope unconditionally — that would import torch on every
    `from semsql_train.trainers.skeleton import ...`, defeating the
    careful lazy-import design above.
    """
    import torch
    import torch.nn.functional as F
    import transformers

    class _DistillationTrainer(transformers.Seq2SeqTrainer):
        """Seq2SeqTrainer with seq-level + token-level KD on top of
        gold supervision."""

        def __init__(self, teacher_model, distillation: DistillationConfig, **kwargs):
            super().__init__(**kwargs)
            self._teacher = teacher_model
            self._kd = distillation

        def compute_loss(self, model, inputs, return_outputs=False, **kwargs):
            # Stock CE loss against gold labels — unchanged from the
            # base Seq2SeqTrainer. `outputs.loss` is the per-token CE
            # the seq-level KD weight α multiplies through (effectively
            # blending α + γ when teacher one-best == gold; the docs
            # cover both interpretations).
            outputs = model(**inputs)
            ce_gold = outputs.loss

            # Token-level KD via the teacher's logits at the same
            # decoder positions. No-grad and on the same device.
            kd_loss = torch.tensor(0.0, device=ce_gold.device)
            if self._kd.beta > 0.0:
                with torch.no_grad():
                    teacher_inputs = {
                        k: v for k, v in inputs.items()
                        if k in {"input_ids", "attention_mask", "labels", "decoder_input_ids"}
                    }
                    teacher_out = self._teacher(**teacher_inputs)
                T = self._kd.temperature
                # Mask padding positions in the labels — KL on padding
                # would inflate the loss with high-confidence pad tokens.
                labels = inputs.get("labels")
                if labels is not None:
                    valid = labels.ne(-100)
                else:
                    valid = None

                student_logits = outputs.logits / T
                teacher_logits = teacher_out.logits / T
                # Vocab-dim KL per position, averaged over valid tokens.
                log_p_student = F.log_softmax(student_logits, dim=-1)
                p_teacher = F.softmax(teacher_logits, dim=-1)
                kl_per_tok = F.kl_div(
                    log_p_student, p_teacher, reduction="none"
                ).sum(-1)  # [B, T]
                if valid is not None:
                    kl_per_tok = kl_per_tok * valid.float()
                    denom = valid.float().sum().clamp_min(1.0)
                    kd_loss = (kl_per_tok.sum() / denom) * (T * T)
                else:
                    kd_loss = kl_per_tok.mean() * (T * T)

            # Final blend per `docs/stage2.md` §4.2.
            loss = (
                (self._kd.alpha + self._kd.gamma) * ce_gold
                + self._kd.beta * kd_loss
            )
            return (loss, outputs) if return_outputs else loss

    return _DistillationTrainer


# Lazy attribute — populated on first access from inside `train_skeleton`.
class _LazyDistillationTrainer:
    _cached = None

    def __call__(self, *args, **kwargs):
        if _LazyDistillationTrainer._cached is None:
            _LazyDistillationTrainer._cached = _build_distillation_trainer_class()
        return _LazyDistillationTrainer._cached(*args, **kwargs)


_DistillationTrainer = _LazyDistillationTrainer()


# ---------------------------------------------------------------------------
# torch-side helpers — only imported inside `train_skeleton`'s code path.
# Defined at module scope so the type is pickle-able by HF Trainer's
# DataLoader workers.
# ---------------------------------------------------------------------------


def _format_source(record: dict) -> str:
    """Render the encoder input per `docs/stage2.md` §2.3.

    Format (v0.3 — FK-aware):

        question: <NL>  ¦  schema:
          <entity>: <field>, <field>, ...
          ...
          FK: <a.id> = <b.a_id>
          FK: ...

    `¦` is the schema sentinel; the M1 student uses the SentencePiece
    tokenizer of the base model verbatim — multi-token fragmentation of
    the sentinel is fine because llguidance binds at decode time, not
    in the encoder.

    FK lines come from `ranked_schema` entries with `kind == "fk"`. The
    `target` is the rendered edge in the form ``"a.id = b.a_id"`` — the
    teacher cache emits one FK per JOIN ON; the generator pulls them
    from the graph's `relationships` table. FK lines are de-duplicated
    while preserving first-seen order so the encoder input is a stable
    function of the record.
    """
    nl = record["nl"]
    by_entity: dict[str, list[str]] = {}
    fk_lines: list[str] = []
    seen_fk: set[str] = set()
    for item in record.get("ranked_schema", []):
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        target = item.get("target")
        if not isinstance(target, str):
            continue
        if kind == "entity":
            by_entity.setdefault(target, [])
        elif kind == "field" and "." in target:
            entity, field = target.split(".", 1)
            by_entity.setdefault(entity, []).append(field)
        elif kind == "fk":
            if target not in seen_fk:
                seen_fk.add(target)
                fk_lines.append(f"FK: {target}")
    schema_lines = []
    for entity, fields in by_entity.items():
        if fields:
            schema_lines.append(f"{entity}: {', '.join(fields)}")
        else:
            schema_lines.append(f"{entity}:")
    schema_lines.extend(fk_lines)
    schema = "\n  ".join(schema_lines) if schema_lines else "(empty)"
    return f"question: {nl}  ¦  schema:\n  {schema}"


def _format_target(record: dict) -> str:
    return record["natsql_skeleton"]


class _SkeletonDataset:
    """Torch-style map-style dataset wrapping the JSONL records.

    Tokenises lazily — keeping memory bounded for large corpora.
    """

    def __init__(
        self,
        examples: list[dict],
        tokenizer,
        max_source_tokens: int,
        max_target_tokens: int,
    ) -> None:
        self.examples = examples
        self.tokenizer = tokenizer
        self.max_source_tokens = max_source_tokens
        self.max_target_tokens = max_target_tokens

    def __len__(self) -> int:
        return len(self.examples)

    def __getitem__(self, idx: int) -> dict:
        rec = self.examples[idx]
        source = _format_source(rec)
        target = _format_target(rec)
        # Use the modern `text_target` kwarg — `as_target_tokenizer()` is
        # deprecated in transformers ≥4.37. The kwarg-form returns the
        # same tokenization shape (`input_ids` + `attention_mask`).
        enc = self.tokenizer(
            source,
            text_target=target,
            max_length=self.max_source_tokens,
            truncation=True,
        )
        if "labels" not in enc:
            # Older transformers route the target output through a
            # separate `labels` field. Newer versions populate it
            # directly. Be tolerant of both.
            tgt = self.tokenizer(
                text_target=target,
                max_length=self.max_target_tokens,
                truncation=True,
            )
            enc["labels"] = tgt["input_ids"]
        return enc


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
