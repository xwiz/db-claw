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
