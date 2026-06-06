from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_script() -> ModuleType:
    script = Path(__file__).resolve().parents[3] / "scripts" / "rehearse_release_packages.py"
    spec = importlib.util.spec_from_file_location("rehearse_release_packages", script)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _write_fixture(root: Path) -> None:
    (root / "Cargo.toml").write_text(
        '[workspace.package]\nversion = "0.1.0-dev"\n',
        encoding="utf-8",
    )
    (root / "package.json").write_text(
        json.dumps({"name": "semsql-monorepo", "version": "0.1.0-dev"}),
        encoding="utf-8",
    )
    module = _load_script()
    for package_dir in module.NPM_RELEASE_PACKAGE_DIRS:
        path = root / "packages" / package_dir
        path.mkdir(parents=True, exist_ok=True)
        (path / "package.json").write_text(
            json.dumps(
                {
                    "name": f"@semsql/{package_dir}",
                    "version": "0.1.0-dev",
                }
            ),
            encoding="utf-8",
        )


def test_rehearsal_applies_alpha_and_restores_version_files(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    module = _load_script()
    _write_fixture(tmp_path)
    original = (tmp_path / "package.json").read_text(encoding="utf-8")
    observed_versions: list[str] = []

    def fake_check_versions(**kwargs: Any) -> dict[str, object]:
        root = kwargs["root"]
        assert isinstance(root, Path)
        observed_versions.append(
            json.loads((root / "package.json").read_text(encoding="utf-8"))[
                "version"
            ]
        )
        return {"status": "pass"}

    def fake_check_packages(**kwargs: Any) -> dict[str, object]:
        root = kwargs["root"]
        assert isinstance(root, Path)
        observed_versions.append(
            json.loads(
                (root / "packages" / "semsql-cli" / "package.json").read_text(
                    encoding="utf-8",
                )
            )["version"]
        )
        return {"status": "pass"}

    monkeypatch.setattr(module, "check_release_versions", fake_check_versions)
    monkeypatch.setattr(module, "check_release_packages", fake_check_packages)

    report = module.rehearse_release_packages(
        root=tmp_path,
        version="0.1.0-alpha.1",
        run_checks=False,
    )

    assert report["status"] == "pass"
    assert report["restored"] is True
    assert observed_versions == ["0.1.0-alpha.1", "0.1.0-alpha.1"]
    assert (tmp_path / "package.json").read_text(encoding="utf-8") == original


def test_rehearsal_reports_restore_failure(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    module = _load_script()
    _write_fixture(tmp_path)

    monkeypatch.setattr(
        module,
        "check_release_versions",
        lambda **_kwargs: {"status": "pass"},
    )
    monkeypatch.setattr(
        module,
        "check_release_packages",
        lambda **_kwargs: {"status": "pass"},
    )

    def broken_restore(_snapshot: object) -> None:
        raise RuntimeError("restore broke")

    monkeypatch.setattr(module, "restore_files", broken_restore)

    report = module.rehearse_release_packages(
        root=tmp_path,
        version="0.1.0-alpha.1",
        run_checks=False,
    )

    assert report["status"] == "fail"
    assert report["restored"] is False
    assert report["restore_error"] == "restore broke"
