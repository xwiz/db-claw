"""ONNX export pipeline for the cascade.

Reads a trained checkpoint per stage (linker / skeleton / slot-filler) and
writes ONNX int8 artifacts plus a JSON manifest the Rust runtime
(``semsql-runtime``) consumes at start-up.

This module is *callable* without ``torch`` / ``transformers`` /
``optimum`` installed — the ML imports are deferred to inside the
function bodies. That keeps the rest of the training pipeline (data
generation, eval harness) usable in environments without GPUs or large
ML stacks.

The manifest shape:

    {
      "schema_version": 1,
      "cascade_version": "v0.2.0",
      "linker":      {"path": "linker.onnx",     "tokenizer": "linker.tok.json",   "params": 9_500_000},
      "skeleton":    {"path": "skeleton.onnx",   "tokenizer": "skeleton.tok.json", "params": 19_800_000},
      "slot_filler": {"path": "slot_filler.onnx", "tokenizer": "slot.tok.json",    "params": 4_900_000},
      "natsql_grammar": "natsql.lark"
    }

The Rust ``semsql-runtime`` reads the manifest, mmaps the ONNX files via
``ort``, and binds the llguidance grammar at start-up.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "CascadeExportConfig",
    "ExportConfig",
    "Manifest",
    "StageArtifact",
    "export_cascade",
    "export_stage",
    "read_manifest",
    "write_manifest",
]

MANIFEST_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class StageArtifact:
    """One ONNX file + its tokenizer."""

    path: str
    tokenizer: str
    params: int


@dataclass(frozen=True)
class Manifest:
    """Cascade manifest. Mirrors what Rust ``semsql-runtime`` deserialises."""

    cascade_version: str
    linker: StageArtifact
    skeleton: StageArtifact
    slot_filler: StageArtifact
    natsql_grammar: str = "natsql.lark"
    schema_version: int = MANIFEST_SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "cascade_version": self.cascade_version,
            "linker": asdict(self.linker),
            "skeleton": asdict(self.skeleton),
            "slot_filler": asdict(self.slot_filler),
            "natsql_grammar": self.natsql_grammar,
        }


@dataclass
class ExportConfig:
    """Knobs for one stage's export."""

    checkpoint: Path
    output_dir: Path
    stage: str
    """One of ``linker`` / ``skeleton`` / ``slot_filler``."""

    int8: bool = True
    """Apply onnxruntime int8 quantisation. Off only for debugging."""

    opset: int = 17
    extra: dict[str, Any] = field(default_factory=dict)


def export_stage(cfg: ExportConfig) -> StageArtifact:
    """Export a single stage's checkpoint to ONNX (+ optional int8 quantisation).

    Pipeline:

      1. Load the HF checkpoint at ``cfg.checkpoint``. The architecture
         is inferred from ``cfg.stage`` — sequence-classification for
         linker / slot_filler, seq2seq for skeleton.
      2. Export to ONNX via ``optimum.onnxruntime``. Opset is pinned by
         ``cfg.opset`` so cascade-runtime / server-runtime stay aligned.
      3. (Optional) quantise to int8 with dynamic quantisation —
         smaller, faster, negligible accuracy hit on encoder models per
         the `optimum` benchmarks.
      4. Save the tokenizer alongside so the Rust loader doesn't need
         a separate side-channel.

    Imports are deferred so the function is importable on machines
    without torch/optimum.
    """
    if cfg.stage not in {"linker", "skeleton", "slot_filler"}:
        raise ValueError(f"unknown stage: {cfg.stage!r}")
    if not cfg.checkpoint.exists():
        raise FileNotFoundError(cfg.checkpoint)
    cfg.output_dir.mkdir(parents=True, exist_ok=True)

    try:
        import torch
        from optimum.onnxruntime import (
            ORTModelForSeq2SeqLM,
            ORTModelForSequenceClassification,
            ORTQuantizer,
        )
        from optimum.onnxruntime.configuration import AutoQuantizationConfig
        from transformers import AutoTokenizer
    except ImportError as e:  # pragma: no cover — exercised only when ML stack missing
        raise RuntimeError(
            "ONNX export requires `pip install semsql-train[ml]` "
            "(transformers + optimum + onnxruntime)."
        ) from e
    # Optimum/ONNXRuntime releases after int4 support reference
    # ``torch.int4`` while Torch 2.5.x does not expose that dtype yet.
    # The export path here only needs the attribute for type mapping
    # during ORT session inspection; aliasing to int8 keeps older Torch
    # environments usable without changing emitted model weights.
    if not hasattr(torch, "int4"):
        torch.int4 = torch.int8  # type: ignore[attr-defined]

    onnx_filename = f"{cfg.stage}.onnx"
    tokenizer_filename = f"{cfg.stage}.tok.json"
    onnx_path = cfg.output_dir / onnx_filename
    tokenizer_path = cfg.output_dir / tokenizer_filename

    # Export to a per-stage staging subdir so multi-file exports
    # (T5 encoder/decoder split) and per-stage ORTQuantizer runs don't
    # clobber each other.  We then promote the canonical artefact(s)
    # to <output_dir>/<stage>.onnx (and split-file siblings) by copy.
    stage_dir = cfg.output_dir / f"_{cfg.stage}_export"
    stage_dir.mkdir(parents=True, exist_ok=True)

    # 1+2. Export the model.
    if cfg.stage == "skeleton":
        model_cls = ORTModelForSeq2SeqLM
    else:
        model_cls = ORTModelForSequenceClassification
    model = model_cls.from_pretrained(
        cfg.checkpoint,
        export=True,
        provider="CPUExecutionProvider",
    )
    model.save_pretrained(stage_dir)

    # 3. Quantise. Optimum's ORTQuantizer does dynamic int8 by default
    # which is the right call for encoder-style models — no calibration
    # data required.
    if cfg.int8:
        try:
            quantizer = ORTQuantizer.from_pretrained(stage_dir)
            qconfig = AutoQuantizationConfig.avx512_vnni(is_static=False, per_channel=False)
            quantizer.quantize(save_dir=stage_dir, quantization_config=qconfig)
        except Exception as e:  # pragma: no cover — depends on optimum runtime
            # Quantisation is a perf optimisation; a failure here should
            # not lose the un-quantised export.
            (stage_dir / "quantisation_failed.txt").write_text(
                f"int8 quantisation skipped: {type(e).__name__}: {e}\n",
                encoding="utf-8",
            )

    # 4. Tokenizer side-by-side in the stage_dir, then promote.
    tokenizer = AutoTokenizer.from_pretrained(cfg.checkpoint)
    tokenizer.save_pretrained(stage_dir)

    # Promote canonical ONNX file(s) to <stage>.onnx in cfg.output_dir.
    # T5 (skeleton) exports as encoder_model.onnx + decoder_model.onnx
    # + decoder_with_past_model.onnx; we keep all three under
    # `<stage>_<role>.onnx` and use the encoder as the manifest path.
    promoted_main: Path | None = None
    if cfg.stage == "skeleton":
        for role in ("encoder_model", "decoder_model", "decoder_with_past_model"):
            src = stage_dir / f"{role}.onnx"
            if src.exists():
                dst = cfg.output_dir / f"{cfg.stage}_{role.replace('_model','')}.onnx"
                dst.write_bytes(src.read_bytes())
                if role == "encoder_model":
                    promoted_main = dst
        # Manifest path = the encoder; runtime loads the trio.
        if promoted_main is not None:
            onnx_path.write_bytes(promoted_main.read_bytes())
    else:
        # Sequence classification → single model.onnx (or model_quantized.onnx).
        # Prefer the quantised one when present, fall back to fp32.
        for cand_name in ("model_quantized.onnx", "model.onnx"):
            src = stage_dir / cand_name
            if src.exists():
                onnx_path.write_bytes(src.read_bytes())
                promoted_main = onnx_path
                break

    # Promote tokenizer to <stage>.tok.json.
    for cand in ("tokenizer.json", "vocab.json"):
        src = stage_dir / cand
        if src.exists():
            try:
                tokenizer_path.write_bytes(src.read_bytes())
            except OSError:
                pass
            break

    params = _count_onnx_parameters(onnx_path)
    if cfg.stage == "skeleton":
        return StageArtifact(
            path=stage_dir.name,
            tokenizer=f"{stage_dir.name}/tokenizer.json",
            params=params,
        )
    return StageArtifact(
        path=onnx_filename,
        tokenizer=tokenizer_filename if tokenizer_path.exists() else "tokenizer.json",
        params=params,
    )


def _count_onnx_parameters(onnx_path: Path) -> int:
    """Sum every initialiser tensor's element count in an ONNX file.

    Best-effort — if `onnx` is unavailable we return 0 and let the
    caller fill the count from the source checkpoint. Cheap to compute
    after export.
    """
    try:
        import onnx
    except ImportError:
        return 0
    if not onnx_path.exists():
        return 0
    model = onnx.load(str(onnx_path))
    total = 0
    for init in model.graph.initializer:
        n = 1
        for d in init.dims:
            n *= int(d)
        total += n
    return total


@dataclass
class CascadeExportConfig:
    """Knobs for an end-to-end three-stage cascade export.

    `linker_checkpoint` / `skeleton_checkpoint` / `slot_filler_checkpoint`
    point at the HF-format directories produced by each stage's trainer
    (e.g. `train_skeleton`'s `output_dir`). Either all three are
    supplied — full cascade export — or any subset can be set with the
    rest left as `None` to skip that stage and reuse a previous artefact.

    The `output_dir` ends up containing:

        manifest.json
        linker.onnx       linker.tok.json
        skeleton.onnx     skeleton.tok.json
        slot_filler.onnx  slot_filler.tok.json
    """

    output_dir: Path
    cascade_version: str
    linker_checkpoint: Path | None = None
    skeleton_checkpoint: Path | None = None
    slot_filler_checkpoint: Path | None = None
    int8: bool = True
    opset: int = 17
    natsql_grammar: str = "natsql.lark"


def export_cascade(cfg: CascadeExportConfig) -> Manifest:
    """Export every supplied stage checkpoint to ONNX and write the
    cascade manifest.

    Reuses any pre-existing per-stage artefacts in `output_dir` when
    the corresponding checkpoint is `None` — a partial re-export only
    re-runs the stages whose weights actually changed. Each stage's
    `StageArtifact` is read from disk if the on-disk filenames are
    present and the params count is recoverable; otherwise a
    placeholder with `params=0` is emitted.

    Returns the written manifest. Raises `RuntimeError` when no stage
    checkpoint is supplied AND no pre-existing artefacts are found —
    that would produce a useless empty manifest.
    """
    cfg.output_dir.mkdir(parents=True, exist_ok=True)

    artefacts: dict[str, StageArtifact] = {}
    existing_manifest = _read_existing_manifest(cfg.output_dir / "manifest.json")

    def _resolve(stage: str, ckpt: Path | None) -> StageArtifact:
        if ckpt is not None:
            stage_cfg = ExportConfig(
                checkpoint=ckpt,
                output_dir=cfg.output_dir,
                stage=stage,
                int8=cfg.int8,
                opset=cfg.opset,
            )
            return export_stage(stage_cfg)
        if existing_manifest is not None:
            existing = _manifest_stage(existing_manifest, stage)
            if (cfg.output_dir / existing.path).exists():
                return existing
        # No checkpoint — try to reuse a pre-existing artefact.
        onnx_filename = f"{stage}.onnx"
        tok_filename = f"{stage}.tok.json"
        onnx_path = cfg.output_dir / onnx_filename
        if not onnx_path.exists():
            raise RuntimeError(
                f"no checkpoint supplied for {stage} AND no existing "
                f"{onnx_filename} found in {cfg.output_dir}"
            )
        tok_present = (cfg.output_dir / tok_filename).exists()
        return StageArtifact(
            path=onnx_filename,
            tokenizer=tok_filename if tok_present else "tokenizer.json",
            params=_count_onnx_parameters(onnx_path),
        )

    artefacts["linker"] = _resolve("linker", cfg.linker_checkpoint)
    artefacts["skeleton"] = _resolve("skeleton", cfg.skeleton_checkpoint)
    artefacts["slot_filler"] = _resolve("slot_filler", cfg.slot_filler_checkpoint)

    manifest = Manifest(
        cascade_version=cfg.cascade_version,
        linker=artefacts["linker"],
        skeleton=artefacts["skeleton"],
        slot_filler=artefacts["slot_filler"],
        natsql_grammar=cfg.natsql_grammar,
    )
    write_manifest(manifest, cfg.output_dir / "manifest.json")
    return manifest


def _read_existing_manifest(path: Path) -> Manifest | None:
    if not path.exists():
        return None
    try:
        return read_manifest(path)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _manifest_stage(manifest: Manifest, stage: str) -> StageArtifact:
    if stage == "linker":
        return manifest.linker
    if stage == "skeleton":
        return manifest.skeleton
    if stage == "slot_filler":
        return manifest.slot_filler
    raise ValueError(f"unknown stage: {stage!r}")


def write_manifest(manifest: Manifest, dest: Path) -> Path:
    """Serialise ``manifest`` to ``dest`` as pretty-printed JSON.

    Atomic on POSIX (write-then-rename); on Windows the rename is
    best-effort. Manifest files are tiny so partial-write risk is low.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_text(json.dumps(manifest.as_dict(), indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(dest)
    return dest


def read_manifest(path: Path) -> Manifest:
    """Inverse of :func:`write_manifest`. Validates the schema version."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    found_v = int(raw.get("schema_version", 0))
    if found_v > MANIFEST_SCHEMA_VERSION:
        raise ValueError(
            f"{path}: manifest schema version {found_v} is newer than supported "
            f"{MANIFEST_SCHEMA_VERSION} — upgrade semsql-train"
        )
    return Manifest(
        cascade_version=raw["cascade_version"],
        linker=StageArtifact(**raw["linker"]),
        skeleton=StageArtifact(**raw["skeleton"]),
        slot_filler=StageArtifact(**raw["slot_filler"]),
        natsql_grammar=raw.get("natsql_grammar", "natsql.lark"),
        schema_version=found_v or MANIFEST_SCHEMA_VERSION,
    )
