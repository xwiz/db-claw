from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType


def _load_script() -> ModuleType:
    script = Path(__file__).resolve().parents[3] / "scripts" / "prepare_release_versions.py"
    spec = importlib.util.spec_from_file_location("prepare_release_versions", script)
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
        json.dumps({"name": "semsql-monorepo", "version": "0.1.0-dev"}, indent=2) + "\n",
        encoding="utf-8",
    )
    module = _load_script()
    for package_dir in module.NPM_RELEASE_PACKAGE_DIRS:
        path = root / "packages" / package_dir
        path.mkdir(parents=True, exist_ok=True)
        (path / "package.json").write_text(
            json.dumps(
                {"name": f"@semsql/{package_dir}", "version": "0.1.0-dev"},
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    py = root / "python" / "semsql_eval"
    py.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "semsql-monorepo"\nversion = "0.1.0.dev0"\n',
        encoding="utf-8",
    )
    (py / "pyproject.toml").write_text(
        '[project]\nname = "semsql-eval"\nversion = "0.1.0.dev0"\n',
        encoding="utf-8",
    )


def test_prepare_release_versions_dry_run_does_not_write(tmp_path: Path) -> None:
    module = _load_script()
    _write_fixture(tmp_path)

    report = module.prepare_release_versions(
        root=tmp_path,
        version="0.1.0-alpha.1",
        apply=False,
    )

    assert report["mode"] == "dry-run"
    assert report["changed_count"] == 11
    assert 'version = "0.1.0-dev"' in (tmp_path / "Cargo.toml").read_text(encoding="utf-8")
    assert json.loads((tmp_path / "package.json").read_text(encoding="utf-8"))[
        "version"
    ] == "0.1.0-dev"


def test_prepare_release_versions_apply_updates_requested_surfaces(tmp_path: Path) -> None:
    module = _load_script()
    _write_fixture(tmp_path)

    report = module.prepare_release_versions(
        root=tmp_path,
        version="0.1.0-alpha.1",
        include_python=True,
        python_version="0.1.0a1",
        apply=True,
    )

    assert report["mode"] == "apply"
    assert report["changed_count"] == 13
    assert 'version = "0.1.0-alpha.1"' in (
        tmp_path / "Cargo.toml"
    ).read_text(encoding="utf-8")
    assert json.loads((tmp_path / "packages" / "semsql-cli" / "package.json").read_text(encoding="utf-8"))[
        "version"
    ] == "0.1.0-alpha.1"
    assert 'version = "0.1.0a1"' in (
        tmp_path / "python" / "semsql_eval" / "pyproject.toml"
    ).read_text(encoding="utf-8")
