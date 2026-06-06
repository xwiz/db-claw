#!/usr/bin/env python3
"""Prepare Rust/npm/Python package versions for a release.

Default mode is a dry run. Pass ``--apply`` to write files.
"""

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

NPM_RUST_VERSION_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
PYTHON_VERSION_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:(?:a|b|rc|\.dev|\.post)\d+)?$"
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


def version_from_args(version: str | None, tag: str | None) -> str:
    if version:
        return version
    if not tag:
        raise ValueError("pass --version or --tag")
    derived = tag.removeprefix("v")
    if not derived:
        raise ValueError("--tag must include a version")
    return derived


def prepare_release_versions(
    *,
    root: Path,
    version: str,
    python_version: str | None = None,
    include_python: bool = False,
    apply: bool = False,
) -> dict[str, Any]:
    _validate_npm_rust_version(version)
    if include_python:
        if python_version is None:
            raise ValueError("--include-python requires --python-version")
        _validate_python_version(python_version)

    changes: list[dict[str, Any]] = []
    changes.append(_prepare_cargo_workspace_version(root / "Cargo.toml", version, apply))
    for path in _npm_package_paths(root):
        changes.append(_prepare_package_json_version(path, version, apply))
    if include_python:
        assert python_version is not None
        for path in _python_project_paths(root):
            changes.append(_prepare_pyproject_version(path, python_version, apply))

    changed = [change for change in changes if change["old_version"] != change["new_version"]]
    return {
        "schema_version": 1,
        "status": "pass",
        "mode": "apply" if apply else "dry-run",
        "version": version,
        "python_version": python_version if include_python else None,
        "include_python": include_python,
        "file_count": len(changes),
        "changed_count": len(changed),
        "changes": changes,
    }


def _npm_package_paths(root: Path) -> list[Path]:
    return [
        root / "package.json",
        *[root / "packages" / package_dir / "package.json" for package_dir in NPM_RELEASE_PACKAGE_DIRS],
    ]


def _python_project_paths(root: Path) -> list[Path]:
    return [root / "pyproject.toml", *sorted((root / "python").glob("*/pyproject.toml"))]


def _prepare_cargo_workspace_version(path: Path, version: str, apply: bool) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    in_workspace_package = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_workspace_package = stripped == "[workspace.package]"
            continue
        if in_workspace_package and stripped.startswith("version"):
            old = _toml_string_value(stripped)
            newline = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            prefix = line[: len(line) - len(line.lstrip())]
            lines[index] = f'{prefix}version = "{version}"{newline}'
            new_text = "".join(lines)
            if apply and new_text != text:
                path.write_text(new_text, encoding="utf-8")
            return _change("rust", path, "workspace.package.version", old, version)
    raise RuntimeError(f"workspace package version not found in {path}")


def _prepare_package_json_version(path: Path, version: str, apply: bool) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    old = str(payload.get("version", ""))
    payload["version"] = version
    if apply and old != version:
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return _change("npm", path, str(payload.get("name", path.name)), old, version)


def _prepare_pyproject_version(path: Path, version: str, apply: bool) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(r"^(\s*version\s*=\s*)(['\"])([^'\"]+)(['\"])(\s*)$", re.MULTILINE)
    match = pattern.search(text)
    if not match:
        raise RuntimeError(f"project version not found in {path}")
    old = match.group(3)
    new_text = text[: match.start()] + (
        f"{match.group(1)}{match.group(2)}{version}{match.group(4)}{match.group(5)}"
    ) + text[match.end() :]
    if apply and new_text != text:
        path.write_text(new_text, encoding="utf-8")
    name = _toml_string(path, "name") or path.parent.name
    return _change("python", path, name, old, version)


def _change(
    surface: str,
    path: Path,
    name: str,
    old_version: str,
    new_version: str,
) -> dict[str, Any]:
    return {
        "surface": surface,
        "path": str(path),
        "name": name,
        "old_version": old_version,
        "new_version": new_version,
        "changed": old_version != new_version,
    }


def _toml_string(path: Path, key: str) -> str | None:
    pattern = re.compile(rf"^{re.escape(key)}\s*=\s*['\"]([^'\"]+)['\"]")
    for line in path.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line.strip())
        if match:
            return match.group(1)
    return None


def _toml_string_value(line: str) -> str:
    _, value = line.split("=", 1)
    value = value.strip()
    if len(value) >= 2 and value[0] in {'"', "'"} and value[-1] == value[0]:
        return value[1:-1]
    raise RuntimeError(f"expected TOML string value, got: {line}")


def _validate_npm_rust_version(version: str) -> None:
    if NPM_RUST_VERSION_RE.fullmatch(version) is None:
        raise ValueError(f"invalid Rust/npm semver version: {version}")


def _validate_python_version(version: str) -> None:
    if PYTHON_VERSION_RE.fullmatch(version) is None:
        raise ValueError(f"invalid Python release version: {version}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", help="Rust/npm version, e.g. 0.1.0-alpha.1.")
    parser.add_argument("--tag", help="Release tag, e.g. v0.1.0-alpha.1.")
    parser.add_argument("--include-python", action="store_true")
    parser.add_argument("--python-version", help="Python version, e.g. 0.1.0a1.")
    parser.add_argument("--apply", action="store_true", help="Write changes. Default is dry-run.")
    parser.add_argument("--out-json", type=Path)
    args = parser.parse_args()

    report = prepare_release_versions(
        root=repo_root(),
        version=version_from_args(args.version, args.tag),
        python_version=args.python_version,
        include_python=args.include_python,
        apply=args.apply,
    )
    rendered = json.dumps(report, indent=2) + "\n"
    if args.out_json is not None:
        args.out_json.parent.mkdir(parents=True, exist_ok=True)
        args.out_json.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
