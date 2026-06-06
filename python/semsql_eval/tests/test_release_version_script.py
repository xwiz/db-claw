from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

from _pytest.capture import CaptureFixture
from _pytest.monkeypatch import MonkeyPatch


def _load_script() -> ModuleType:
    script = Path(__file__).resolve().parents[3] / "scripts" / "check_release_versions.py"
    spec = importlib.util.spec_from_file_location("check_release_versions", script)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_expected_version_from_tag() -> None:
    module = _load_script()
    assert module.expected_version_from_args("v0.1.0-alpha.1", None) == "0.1.0-alpha.1"
    assert module.expected_version_from_args("v0.1.0", "0.2.0") == "0.2.0"


def test_release_version_from_tag_accepts_non_dev_semver() -> None:
    module = _load_script()

    assert module.release_version_from_tag("v0.1.0-alpha.1") == "0.1.0-alpha.1"
    assert module.release_version_from_tag("v1.2.3") == "1.2.3"


def test_release_version_from_tag_rejects_malformed_or_dev_tags() -> None:
    module = _load_script()

    for tag in [None, "0.1.0-alpha.1", "v0.1", "v0.1.0-dev", "v0.1.0-dev.1"]:
        try:
            module.release_version_from_tag(tag)
        except ValueError:
            continue
        raise AssertionError(f"expected {tag!r} to be rejected")


def test_validate_release_tag_report_returns_structured_payload() -> None:
    module = _load_script()

    report = module.validate_release_tag_report("v0.1.0-alpha.1")

    assert report["status"] == "pass"
    assert report["version"] == "0.1.0-alpha.1"


def test_validate_release_tag_only_cli_reports_clean_failure(
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    module = _load_script()
    monkeypatch.setattr(
        sys,
        "argv",
        ["check_release_versions.py", "--tag", "v0.1.0-dev", "--validate-release-tag-only"],
    )

    assert module.main() == 1
    report = json.loads(capsys.readouterr().out)

    assert report["status"] == "fail"
    assert "dev version" in report["error"]


def test_release_version_check_detects_rust_and_npm_mismatch(tmp_path: Path) -> None:
    module = _load_script()
    (tmp_path / "packages" / "semsql-cli").mkdir(parents=True)
    for package_dir in module.NPM_RELEASE_PACKAGE_DIRS:
        package_path = tmp_path / "packages" / package_dir
        package_path.mkdir(parents=True, exist_ok=True)
        (package_path / "package.json").write_text(
            json.dumps(
                {
                    "name": f"@semsql/{package_dir}",
                    "version": "0.1.0-dev",
                }
            ),
            encoding="utf-8",
        )
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "semsql-monorepo", "version": "0.1.0-dev"}),
        encoding="utf-8",
    )
    (tmp_path / "Cargo.toml").write_text(
        '[workspace.package]\nversion = "0.1.0-dev"\n',
        encoding="utf-8",
    )

    report = module.check_release_versions(
        root=tmp_path,
        expected_version="0.1.0-alpha.1",
        surfaces=("rust", "npm"),
    )

    assert report["status"] == "fail"
    assert {item["surface"] for item in report["mismatches"]} == {"rust", "npm"}
