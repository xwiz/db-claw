from __future__ import annotations

from pathlib import Path

import pytest

from semsql_train.onnx_export import (
    ExportConfig,
    Manifest,
    MANIFEST_SCHEMA_VERSION,
    StageArtifact,
    export_stage,
    read_manifest,
    write_manifest,
)


def _sample_manifest() -> Manifest:
    return Manifest(
        cascade_version="v0.2.0-test",
        linker=StageArtifact("linker.onnx", "linker.tok.json", 9_500_000),
        skeleton=StageArtifact("skeleton.onnx", "skeleton.tok.json", 19_800_000),
        slot_filler=StageArtifact("slot.onnx", "slot.tok.json", 4_900_000),
    )


class TestManifest:
    def test_round_trip(self, tmp_path: Path) -> None:
        m = _sample_manifest()
        dest = tmp_path / "manifest.json"
        write_manifest(m, dest)
        loaded = read_manifest(dest)
        assert loaded == m

    def test_includes_schema_version(self, tmp_path: Path) -> None:
        m = _sample_manifest()
        dest = tmp_path / "m.json"
        write_manifest(m, dest)
        text = dest.read_text(encoding="utf-8")
        assert f'"schema_version": {MANIFEST_SCHEMA_VERSION}' in text

    def test_rejects_too_new_version(self, tmp_path: Path) -> None:
        path = tmp_path / "m.json"
        m = _sample_manifest().as_dict()
        m["schema_version"] = MANIFEST_SCHEMA_VERSION + 1
        import json

        path.write_text(json.dumps(m), encoding="utf-8")
        with pytest.raises(ValueError):
            read_manifest(path)


class TestExportStage:
    def test_unknown_stage_rejected(self, tmp_path: Path) -> None:
        ckpt = tmp_path / "ckpt.bin"
        ckpt.write_bytes(b"")
        cfg = ExportConfig(checkpoint=ckpt, output_dir=tmp_path, stage="bogus")
        with pytest.raises(ValueError):
            export_stage(cfg)

    def test_missing_checkpoint_raises(self, tmp_path: Path) -> None:
        cfg = ExportConfig(
            checkpoint=tmp_path / "missing.bin", output_dir=tmp_path, stage="linker"
        )
        with pytest.raises(FileNotFoundError):
            export_stage(cfg)


class TestExportCascade:
    def test_writes_manifest_when_reusing_pre_existing_artefacts(
        self, tmp_path: Path
    ) -> None:
        """When no checkpoints are supplied, `export_cascade` reuses
        ONNX files already in `output_dir` and emits a manifest from
        their on-disk state. This covers the common partial-re-export
        flow — Stage 2 freshly trained, Stage 1 + 3 weights unchanged."""
        from semsql_train.onnx_export import (
            CascadeExportConfig,
            export_cascade,
        )

        out = tmp_path / "cascade"
        out.mkdir()
        # Pre-populate three placeholder ONNX files + tokenizer
        # filenames matching the manifest convention.
        for stage in ("linker", "skeleton", "slot_filler"):
            (out / f"{stage}.onnx").write_bytes(b"")
            (out / f"{stage}.tok.json").write_bytes(b"")

        cfg = CascadeExportConfig(
            output_dir=out, cascade_version="v0.5.0-test"
        )
        manifest = export_cascade(cfg)
        assert manifest.cascade_version == "v0.5.0-test"
        assert manifest.linker.path == "linker.onnx"
        assert manifest.skeleton.path == "skeleton.onnx"
        assert manifest.slot_filler.path == "slot_filler.onnx"
        # Manifest is also written to disk and reads back identically.
        on_disk = read_manifest(out / "manifest.json")
        assert on_disk.cascade_version == "v0.5.0-test"
        assert on_disk.linker.tokenizer == "linker.tok.json"

    def test_raises_when_no_checkpoint_and_no_artefact(
        self, tmp_path: Path
    ) -> None:
        """A stage with no supplied checkpoint AND no pre-existing
        ONNX file is a configuration error — the manifest would point
        at a non-existent artefact."""
        from semsql_train.onnx_export import (
            CascadeExportConfig,
            export_cascade,
        )

        out = tmp_path / "cascade"
        out.mkdir()
        cfg = CascadeExportConfig(output_dir=out, cascade_version="v0")
        with pytest.raises(RuntimeError, match="no checkpoint supplied for linker"):
            export_cascade(cfg)

    def test_falls_back_to_canonical_tokenizer_when_per_stage_missing(
        self, tmp_path: Path
    ) -> None:
        """Some checkpoint-export paths leave only the canonical
        ``tokenizer.json`` in the output dir (no per-stage rename).
        The reuse path should record the fallback name in the
        manifest so the Rust loader still finds it."""
        from semsql_train.onnx_export import (
            CascadeExportConfig,
            export_cascade,
        )

        out = tmp_path / "cascade"
        out.mkdir()
        for stage in ("linker", "skeleton", "slot_filler"):
            (out / f"{stage}.onnx").write_bytes(b"")
        # No per-stage tokenizer files; only the canonical fallback.
        (out / "tokenizer.json").write_bytes(b"")

        cfg = CascadeExportConfig(output_dir=out, cascade_version="v0")
        manifest = export_cascade(cfg)
        assert manifest.linker.tokenizer == "tokenizer.json"
        assert manifest.skeleton.tokenizer == "tokenizer.json"
