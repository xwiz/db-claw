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
    "StageArtifact",
    "Manifest",
    "MANIFEST_SCHEMA_VERSION",
    "ExportConfig",
    "export_stage",
    "write_manifest",
    "read_manifest",
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

    onnx_filename = f"{cfg.stage}.onnx"
    tokenizer_filename = f"{cfg.stage}.tok.json"
    onnx_path = cfg.output_dir / onnx_filename
    tokenizer_path = cfg.output_dir / tokenizer_filename

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
    model.save_pretrained(cfg.output_dir)

    # 3. Quantise. Optimum's ORTQuantizer does dynamic int8 by default
    # which is the right call for encoder-style models — no calibration
    # data required.
    if cfg.int8:
        try:
            quantizer = ORTQuantizer.from_pretrained(cfg.output_dir)
            qconfig = AutoQuantizationConfig.avx512_vnni(is_static=False, per_channel=False)
            quantizer.quantize(save_dir=cfg.output_dir, quantization_config=qconfig)
        except Exception as e:  # pragma: no cover — depends on optimum runtime
            # Quantisation is a perf optimisation; a failure here should
            # not lose the un-quantised export.
            (cfg.output_dir / "quantisation_failed.txt").write_text(
                f"int8 quantisation skipped: {type(e).__name__}: {e}\n",
                encoding="utf-8",
            )

    # 4. Tokenizer side-by-side.
    tokenizer = AutoTokenizer.from_pretrained(cfg.checkpoint)
    tokenizer.save_pretrained(cfg.output_dir)
    if not tokenizer_path.exists():
        # Some tokenizers save under a different filename — link the
        # canonical one so the manifest lookup is stable.
        for cand in ("tokenizer.json", "vocab.json"):
            src = cfg.output_dir / cand
            if src.exists():
                try:
                    tokenizer_path.write_bytes(src.read_bytes())
                except OSError:
                    pass
                break

    params = _count_onnx_parameters(onnx_path)
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
        import onnx  # type: ignore[import-not-found]
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
