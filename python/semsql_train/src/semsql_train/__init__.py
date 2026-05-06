"""SemanticSQL training pipeline.

Phase 2 of the architecture: combinatorial NL→SQL data generation, per-stage
distillation, and ONNX export.

Per-stage outputs:

- ``generate_linker``: ``(NL, schema_item, relevance_label)`` triples for
  Stage 1 (cross-encoder).
- ``generate_skeleton``: ``(NL + ranked_schema, NatSQL_skeleton)`` pairs for
  Stage 2 (encoder-decoder).
- ``generate_slots``: ``(NL + skeleton + candidates, correct_slot_value)``
  tuples for Stage 3 (classifier).
- ``generate_e2e``: full NL → SQL pairs for end-to-end evaluation only —
  *never* used as training input.

The actual implementations land alongside the cascade weights in v0.2; this
package ships the public surface so eval/inference can already depend on it.
"""

__version__ = "0.1.0.dev0"
