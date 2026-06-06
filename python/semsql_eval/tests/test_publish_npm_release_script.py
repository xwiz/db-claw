from __future__ import annotations

import importlib.util
import io
import json
import tarfile
from pathlib import Path
from types import ModuleType

import pytest


def _load_script() -> ModuleType:
    script = Path(__file__).resolve().parents[3] / "scripts" / "publish_npm_release_packages.py"
    spec = importlib.util.spec_from_file_location("publish_npm_release_packages", script)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _write_tarball(path: Path, name: str, version: str) -> None:
    package_json = json.dumps({"name": name, "version": version}).encode("utf-8")
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo("package/package.json")
        info.size = len(package_json)
        archive.addfile(info, io.BytesIO(package_json))


def test_default_dist_tag_uses_next_for_prereleases() -> None:
    module = _load_script()
    assert module.default_dist_tag("0.1.0-alpha.1") == "next"
    assert module.default_dist_tag("0.1.0") == "latest"


def test_publish_release_packages_dry_run_uses_dependency_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_script()
    package_dir = tmp_path / "dist" / "npm"
    package_dir.mkdir(parents=True)
    for index, package in enumerate(reversed(module.RELEASE_PACKAGE_ORDER)):
        _write_tarball(
            package_dir / f"pkg-{index}.tgz",
            package,
            "0.1.0-alpha.1",
        )

    monkeypatch.chdir(tmp_path)
    report = module.publish_release_packages(
        package_dir=Path("dist/npm"),
        expected_version="0.1.0-alpha.1",
        npm_bin="python",
        dry_run=True,
    )

    assert report["status"] == "pass"
    assert report["dist_tag"] == "next"
    assert [item["name"] for item in report["results"]] == list(module.RELEASE_PACKAGE_ORDER)
    assert {item["action"] for item in report["results"]} == {"dry-run"}
    assert all(Path(item["publish"]["args"][2]).is_absolute() for item in report["results"])


def test_ordered_tarballs_rejects_missing_package(tmp_path: Path) -> None:
    module = _load_script()
    _write_tarball(tmp_path / "only-cli.tgz", "@semsql/cli", "0.1.0-alpha.1")

    try:
        module._ordered_tarballs(tmp_path, "0.1.0-alpha.1")
    except RuntimeError as error:
        assert "@semsql/extractor-sdk" in str(error)
    else:
        raise AssertionError("expected missing tarball failure")
