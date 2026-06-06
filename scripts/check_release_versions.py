#!/usr/bin/env python3
"""Check release-version coherence across ship surfaces."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

NPM_RELEASE_PACKAGE_DIRS = (
    "extractor-sdk",
    "extractor-i18n",
    "extractor-django",
    "extractor-laravel",
    "extractor-nextjs",
    "extractor-rails",
    "extractor-vue",
    "extractor-cli",
    "semsql-cli",
)

DEFAULT_SURFACES = ("rust", "npm")
RELEASE_VERSION_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)


def repo_root() -> Path:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git executable not found")
    proc = subprocess.run(
        [git, "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(proc.stdout.strip()).resolve()


def expected_version_from_args(tag: str | None, expected_version: str | None) -> str:
    if expected_version:
        return expected_version
    if not tag:
        raise ValueError("pass --tag or --expected-version")
    version = tag.removeprefix("v")
    if not version:
        raise ValueError("--tag must include a version")
    return version


def release_version_from_tag(tag: str | None) -> str:
    if not tag:
        raise ValueError("pass --tag")
    if not tag.startswith("v"):
        raise ValueError("release tag must start with v")
    version = tag[1:]
    if RELEASE_VERSION_RE.fullmatch(version) is None:
        raise ValueError(f"invalid release semver: {version}")
    prerelease = version.split("+", 1)[0].split("-", 1)
    prerelease_parts = prerelease[1].split(".") if len(prerelease) == 2 else []
    if any(part.lower().startswith("dev") for part in prerelease_parts):
        raise ValueError(f"release tag must not use a dev version: {version}")
    return version


def validate_release_tag_report(tag: str | None) -> dict[str, Any]:
    version = release_version_from_tag(tag)
    return {
        "schema_version": 1,
        "status": "pass",
        "tag": tag,
        "version": version,
        "checks": [
            "starts_with_v",
            "semver",
            "not_dev_prerelease",
        ],
    }


def check_release_versions(
    *,
    root: Path,
    expected_version: str,
    surfaces: tuple[str, ...] = DEFAULT_SURFACES,
    python_expected_version: str | None = None,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    if "rust" in surfaces:
        checks.append(
            _check_value(
                surface="rust",
                path=root / "Cargo.toml",
                name="workspace.package.version",
                actual=_workspace_cargo_version(root / "Cargo.toml"),
                expected=expected_version,
            )
        )
    if "npm" in surfaces:
        checks.extend(_npm_checks(root, expected_version))
    if "python" in surfaces:
        checks.extend(_python_checks(root, python_expected_version or expected_version))
    mismatches = [check for check in checks if not check["ok"]]
    return {
        "schema_version": 1,
        "status": "pass" if not mismatches else "fail",
        "expected_version": expected_version,
        "python_expected_version": python_expected_version,
        "surfaces": list(surfaces),
        "checks": checks,
        "mismatches": mismatches,
    }


def _npm_checks(root: Path, expected_version: str) -> list[dict[str, Any]]:
    checks = [
        _check_value(
            surface="npm",
            path=root / "package.json",
            name="semsql-monorepo",
            actual=_json_version(root / "package.json"),
            expected=expected_version,
        )
    ]
    for package_dir in NPM_RELEASE_PACKAGE_DIRS:
        path = root / "packages" / package_dir / "package.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        checks.append(
            _check_value(
                surface="npm",
                path=path,
                name=str(payload.get("name")),
                actual=str(payload.get("version")),
                expected=expected_version,
            )
        )
    return checks


def _python_checks(root: Path, expected_version: str) -> list[dict[str, Any]]:
    paths = [root / "pyproject.toml", *sorted((root / "python").glob("*/pyproject.toml"))]
    return [
        _check_value(
            surface="python",
            path=path,
            name=_toml_string(path, "name") or path.parent.name,
            actual=_toml_string(path, "version") or "",
            expected=expected_version,
        )
        for path in paths
    ]


def _check_value(
    *,
    surface: str,
    path: Path,
    name: str,
    actual: str,
    expected: str,
) -> dict[str, Any]:
    return {
        "surface": surface,
        "path": str(path),
        "name": name,
        "actual": actual,
        "expected": expected,
        "ok": actual == expected,
    }


def _workspace_cargo_version(path: Path) -> str:
    in_workspace_package = False
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            in_workspace_package = line == "[workspace.package]"
            continue
        if in_workspace_package and line.startswith("version"):
            return _split_toml_string(line)
    raise RuntimeError(f"workspace package version not found in {path}")


def _json_version(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return str(payload.get("version"))


def _toml_string(path: Path, key: str) -> str | None:
    pattern = re.compile(rf"^{re.escape(key)}\s*=\s*['\"]([^'\"]+)['\"]")
    for line in path.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line.strip())
        if match:
            return match.group(1)
    return None


def _split_toml_string(line: str) -> str:
    _, value = line.split("=", 1)
    value = value.strip()
    if len(value) >= 2 and value[0] in {'"', "'"} and value[-1] == value[0]:
        return value[1:-1]
    raise RuntimeError(f"expected TOML string value, got: {line}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", help="Release tag, for example v0.1.0-alpha.1.")
    parser.add_argument("--expected-version", help="Expected Rust/npm version.")
    parser.add_argument(
        "--validate-release-tag-only",
        action="store_true",
        help="Validate --tag as a publishable non-dev release tag and exit.",
    )
    parser.add_argument(
        "--python-expected-version",
        help="Expected Python package version when --surface python is used.",
    )
    parser.add_argument(
        "--surface",
        action="append",
        choices=("rust", "npm", "python"),
        help="Surface to check. Repeatable. Defaults to rust+npm.",
    )
    parser.add_argument("--out-json", type=Path)
    args = parser.parse_args()

    if args.validate_release_tag_only:
        try:
            report = validate_release_tag_report(args.tag)
        except ValueError as exc:
            report = {
                "schema_version": 1,
                "status": "fail",
                "tag": args.tag,
                "error": str(exc),
            }
        rendered = json.dumps(report, indent=2) + "\n"
        if args.out_json is not None:
            args.out_json.parent.mkdir(parents=True, exist_ok=True)
            args.out_json.write_text(rendered, encoding="utf-8")
        print(rendered, end="")
        return 0 if report["status"] == "pass" else 1

    expected_version = expected_version_from_args(args.tag, args.expected_version)
    report = check_release_versions(
        root=repo_root(),
        expected_version=expected_version,
        surfaces=tuple(args.surface or DEFAULT_SURFACES),
        python_expected_version=args.python_expected_version,
    )
    rendered = json.dumps(report, indent=2) + "\n"
    if args.out_json is not None:
        args.out_json.parent.mkdir(parents=True, exist_ok=True)
        args.out_json.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
