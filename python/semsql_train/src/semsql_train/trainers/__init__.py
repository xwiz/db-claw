"""Per-stage trainers.

Each module in this package fine-tunes one cascade stage:

- :mod:`linker` — Stage 1 cross-encoder.
- :mod:`skeleton` — Stage 2 encoder-decoder (placeholder; lands with weights).
- :mod:`slot_filler` — Stage 3 classifier (placeholder; lands with weights).

The trainers import torch / transformers / peft *lazily* so the rest of
the package (data generation, ONNX manifest, eval harness) runs without a
heavyweight ML stack.
"""

from .linker import LinkerTrainConfig, train_linker

__all__ = ["LinkerTrainConfig", "train_linker"]
