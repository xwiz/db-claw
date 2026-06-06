from __future__ import annotations

import importlib.util
import io
import json
import tarfile
from pathlib import Path
from types import ModuleType


def _load_script() -> ModuleType:
    script = Path(__file__).resolve().parents[3] / "scripts" / "check_npm_release_packages.py"
    spec = importlib.util.spec_from_file_location("check_npm_release_packages", script)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_expected_version_from_tag() -> None:
    module = _load_script()
    assert module.expected_version_from_args("v0.1.0-alpha.1", None) == "0.1.0-alpha.1"
    assert module.expected_version_from_args("v0.1.0", "0.2.0") == "0.2.0"


def test_tarball_inspection_flags_workspace_dependency(tmp_path: Path) -> None:
    module = _load_script()
    tarball = tmp_path / "pkg.tgz"
    package_json = json.dumps(
        {
            "name": "@semsql/example",
            "version": "0.1.0",
            "dependencies": {"@semsql/extractor-sdk": "workspace:*"},
        }
    ).encode("utf-8")
    with tarfile.open(tarball, "w:gz") as archive:
        info = tarfile.TarInfo("package/package.json")
        info.size = len(package_json)
        archive.addfile(info, io.BytesIO(package_json))

    report = module._inspect_tarball(tarball, expected_version="0.1.0")
    assert report["workspace_violations"] == [
        {
            "tarball": str(tarball),
            "section": "dependencies",
            "dependency": "@semsql/extractor-sdk",
            "version": "workspace:*",
        }
    ]


def test_tarball_inspection_flags_runtime_dev_version_literal_for_release(
    tmp_path: Path,
) -> None:
    module = _load_script()
    tarball = tmp_path / "pkg.tgz"
    package_json = json.dumps(
        {"name": "@semsql/example", "version": "0.1.0-alpha.1"}
    ).encode("utf-8")
    runtime = b'export const VERSION = "0.1.0-dev";'
    with tarfile.open(tarball, "w:gz") as archive:
        info = tarfile.TarInfo("package/package.json")
        info.size = len(package_json)
        archive.addfile(info, io.BytesIO(package_json))
        runtime_info = tarfile.TarInfo("package/dist/index.js")
        runtime_info.size = len(runtime)
        archive.addfile(runtime_info, io.BytesIO(runtime))

    report = module._inspect_tarball(tarball, expected_version="0.1.0-alpha.1")

    assert report["dev_version_literal_violations"] == [
        {
            "tarball": str(tarball),
            "member": "package/dist/index.js",
            "literal": "0.1.0-dev",
            "expected": "0.1.0-alpha.1",
        }
    ]


def test_tarball_inspection_rejects_dev_version_literal_for_dev_package_runtime(
    tmp_path: Path,
) -> None:
    module = _load_script()
    tarball = tmp_path / "pkg.tgz"
    package_json = json.dumps(
        {"name": "@semsql/example", "version": "0.1.0-dev"}
    ).encode("utf-8")
    runtime = b'export const VERSION = "0.1.0-dev";'
    with tarfile.open(tarball, "w:gz") as archive:
        info = tarfile.TarInfo("package/package.json")
        info.size = len(package_json)
        archive.addfile(info, io.BytesIO(package_json))
        runtime_info = tarfile.TarInfo("package/dist/index.js")
        runtime_info.size = len(runtime)
        archive.addfile(runtime_info, io.BytesIO(runtime))

    report = module._inspect_tarball(tarball, expected_version="0.1.0-dev")

    assert report["dev_version_literal_violations"] == [
        {
            "tarball": str(tarball),
            "member": "package/dist/index.js",
            "literal": "0.1.0-dev",
            "expected": "0.1.0-dev",
        }
    ]


def test_release_metadata_violations_require_repository_directory() -> None:
    module = _load_script()

    missing = module._release_metadata_violations(
        {
            "dir": "extractor-sdk",
            "name": "@semsql/extractor-sdk",
            "repository": None,
        }
    )
    wrong_directory = module._release_metadata_violations(
        {
            "dir": "extractor-sdk",
            "name": "@semsql/extractor-sdk",
            "repository": {
                "type": "git",
                "url": module.REPOSITORY_URL,
                "directory": "packages/other",
            },
        }
    )
    ok = module._release_metadata_violations(
        {
            "dir": "extractor-sdk",
            "name": "@semsql/extractor-sdk",
            "repository": {
                "type": "git",
                "url": module.REPOSITORY_URL,
                "directory": "packages/extractor-sdk",
            },
        }
    )

    assert missing[0]["field"] == "repository"
    assert wrong_directory == [
        {
            "dir": "extractor-sdk",
            "name": "@semsql/extractor-sdk",
            "field": "repository.directory",
            "actual": "packages/other",
            "expected": "packages/extractor-sdk",
        }
    ]
    assert ok == []


def test_release_metadata_violations_reject_private_release_package() -> None:
    module = _load_script()

    violations = module._release_metadata_violations(
        {
            "dir": "extractor-sdk",
            "name": "@semsql/extractor-sdk",
            "private": True,
            "repository": {
                "type": "git",
                "url": module.REPOSITORY_URL,
                "directory": "packages/extractor-sdk",
            },
        }
    )

    assert {
        "dir": "extractor-sdk",
        "name": "@semsql/extractor-sdk",
        "field": "private",
        "actual": "true",
        "expected": "false",
    } in violations


def test_release_scope_violations_reject_untracked_public_package() -> None:
    module = _load_script()

    violations = module._release_scope_violations(
        packages=[{"dir": "semsql-cli", "name": "@semsql/cli"}],
        workspace_packages=[
            {
                "dir": "semsql-cli",
                "name": "@semsql/cli",
                "private": False,
            },
            {
                "dir": "sheets",
                "name": "@semsql/sheets",
                "private": False,
                "publishConfig": {"access": "public"},
            },
            {
                "dir": "sheets-demo",
                "name": "@semsql/sheets-demo",
                "private": True,
            },
        ],
    )

    assert violations == [
        {
            "dir": "sheets",
            "name": "@semsql/sheets",
            "field": "release_scope",
            "actual": "public workspace package omitted from release set",
            "expected": "mark private or add to RELEASE_PACKAGE_DIRS",
        }
    ]
