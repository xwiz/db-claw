#!/usr/bin/env python3
"""Validate and optionally pack the npm packages used in a semsql release."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Any

RELEASE_PACKAGE_DIRS = (
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

REPOSITORY_URL = "git+https://github.com/xwiz/db-claw.git"

DEPENDENCY_SECTIONS = (
    "dependencies",
    "devDependencies",
    "optionalDependencies",
    "peerDependencies",
)
DEV_VERSION_LITERAL = "0.1.0-dev"
RUNTIME_MEMBER_PREFIXES = ("package/dist/",)
RUNTIME_MEMBER_SUFFIXES = (".js", ".cjs", ".mjs", ".d.ts", ".map")


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


def check_release_packages(
    *,
    root: Path,
    expected_version: str,
    package_manager: str = "pnpm",
    run_checks: bool = False,
    pack_destination: Path | None = None,
    clean_pack_destination: bool = False,
) -> dict[str, Any]:
    package_manager_path = shutil.which(package_manager)
    if package_manager_path is None:
        raise RuntimeError(f"{package_manager} executable not found")

    packages = [_read_package(root, name) for name in RELEASE_PACKAGE_DIRS]
    workspace_packages = _read_workspace_packages(root)
    version_mismatches = [
        {
            "dir": package["dir"],
            "name": package["name"],
            "version": package["version"],
            "expected": expected_version,
        }
        for package in packages
        if package["version"] != expected_version
    ]
    metadata_violations = [
        violation
        for package in packages
        for violation in _release_metadata_violations(package)
    ]
    release_scope_violations = _release_scope_violations(
        packages=packages,
        workspace_packages=workspace_packages,
    )

    command_results: list[dict[str, Any]] = []
    tarballs: list[Path] = []
    if pack_destination is not None:
        destination = pack_destination.resolve()
        if clean_pack_destination:
            _safe_reset_pack_destination(root, destination)
        destination.mkdir(parents=True, exist_ok=True)
        for package in packages:
            package_dir = root / "packages" / str(package["dir"])
            if run_checks:
                for script in ("lint", "test", "typecheck"):
                    command_results.append(
                        _run(
                            [
                                package_manager_path,
                                "--dir",
                                str(package_dir),
                                script,
                            ],
                            cwd=root,
                        )
                    )
            before = set(destination.glob("*.tgz"))
            command_results.append(
                _run(
                    [
                        package_manager_path,
                        "--dir",
                        str(package_dir),
                        "pack",
                        "--pack-destination",
                        str(destination),
                    ],
                    cwd=root,
                )
            )
            after = set(destination.glob("*.tgz"))
            created = sorted(after - before, key=lambda item: item.name)
            if created:
                tarballs.extend(created)
            else:
                expected_stem = _expected_tarball_stem(
                    str(package["name"]),
                    str(package["version"]),
                )
                matches = sorted(destination.glob(f"{expected_stem}.tgz"))
                tarballs.extend(matches)

    tarball_reports = [
        _inspect_tarball(path, expected_version=expected_version)
        for path in sorted(set(tarballs))
    ]
    workspace_violations = [
        violation
        for report in tarball_reports
        for violation in report["workspace_violations"]
    ]
    dev_version_literal_violations = [
        violation
        for report in tarball_reports
        for violation in report["dev_version_literal_violations"]
    ]
    command_failures = [
        result for result in command_results if result["returncode"] != 0
    ]

    status = (
        "pass"
        if not version_mismatches
        and not metadata_violations
        and not release_scope_violations
        and not command_failures
        and not workspace_violations
        and not dev_version_literal_violations
        and (pack_destination is None or len(tarball_reports) == len(RELEASE_PACKAGE_DIRS))
        else "fail"
    )
    return {
        "schema_version": 1,
        "status": status,
        "expected_version": expected_version,
        "package_count": len(packages),
        "packages": packages,
        "workspace_package_count": len(workspace_packages),
        "version_mismatches": version_mismatches,
        "metadata_violations": metadata_violations,
        "release_scope_violations": release_scope_violations,
        "run_checks": run_checks,
        "pack_destination": str(pack_destination) if pack_destination else None,
        "tarball_count": len(tarball_reports),
        "tarballs": tarball_reports,
        "workspace_violations": workspace_violations,
        "dev_version_literal_violations": dev_version_literal_violations,
        "command_failures": [_summarize_command(result) for result in command_failures],
    }


def _read_package(root: Path, name: str) -> dict[str, Any]:
    package_path = root / "packages" / name / "package.json"
    return _read_package_json(package_path, name)


def _read_workspace_packages(root: Path) -> list[dict[str, Any]]:
    package_root = root / "packages"
    if not package_root.exists():
        return []
    return [
        _read_package_json(package_path, package_path.parent.name)
        for package_path in sorted(package_root.glob("*/package.json"))
    ]


def _read_package_json(package_path: Path, directory_name: str) -> dict[str, Any]:
    payload = json.loads(package_path.read_text(encoding="utf-8"))
    repository = payload.get("repository")
    publish_config = payload.get("publishConfig")
    return {
        "dir": directory_name,
        "name": payload.get("name"),
        "version": payload.get("version"),
        "private": bool(payload.get("private", False)),
        "repository": repository if isinstance(repository, dict) else None,
        "publishConfig": publish_config if isinstance(publish_config, dict) else None,
    }


def _release_metadata_violations(package: dict[str, Any]) -> list[dict[str, str]]:
    repository = package.get("repository")
    expected_directory = f"packages/{package['dir']}"
    violations: list[dict[str, str]] = []
    if package.get("private") is True:
        violations.append(
            {
                "dir": str(package["dir"]),
                "name": str(package["name"]),
                "field": "private",
                "actual": "true",
                "expected": "false",
            }
        )
    if not isinstance(repository, dict):
        violations.append(
            {
                "dir": str(package["dir"]),
                "name": str(package["name"]),
                "field": "repository",
                "actual": "",
                "expected": REPOSITORY_URL,
            }
        )
        return violations
    checks = {
        "repository.type": ("git", repository.get("type")),
        "repository.url": (REPOSITORY_URL, repository.get("url")),
        "repository.directory": (expected_directory, repository.get("directory")),
    }
    for field, (expected, actual) in checks.items():
        if actual != expected:
            violations.append(
                {
                    "dir": str(package["dir"]),
                    "name": str(package["name"]),
                    "field": field,
                    "actual": str(actual or ""),
                    "expected": expected,
                }
            )
    return violations


def _release_scope_violations(
    *,
    packages: list[dict[str, Any]],
    workspace_packages: list[dict[str, Any]],
) -> list[dict[str, str]]:
    release_dirs = {str(package["dir"]) for package in packages}
    violations: list[dict[str, str]] = []
    for package in workspace_packages:
        if str(package["dir"]) in release_dirs:
            continue
        if _is_public_workspace_package(package):
            violations.append(
                {
                    "dir": str(package["dir"]),
                    "name": str(package["name"]),
                    "field": "release_scope",
                    "actual": "public workspace package omitted from release set",
                    "expected": "mark private or add to RELEASE_PACKAGE_DIRS",
                }
            )
    return violations


def _is_public_workspace_package(package: dict[str, Any]) -> bool:
    if package.get("private") is True:
        return False
    name = str(package.get("name") or "")
    publish_config = package.get("publishConfig")
    publish_access = (
        str(publish_config.get("access") or "")
        if isinstance(publish_config, dict)
        else ""
    )
    return name.startswith("@semsql/") or publish_access == "public"


def _run(args: list[str], *, cwd: Path) -> dict[str, Any]:
    proc = subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return {
        "args": args,
        "returncode": proc.returncode,
        "stdout_tail": proc.stdout[-2000:],
        "stderr_tail": proc.stderr[-2000:],
    }


def _safe_reset_pack_destination(root: Path, destination: Path) -> None:
    root = root.resolve()
    allowed_roots = [root / "target", root / "dist"]
    if not any(
        destination == allowed_root or allowed_root in destination.parents
        for allowed_root in allowed_roots
    ):
        raise RuntimeError(
            "refusing to clean pack destination outside target/ or dist/: "
            f"{destination}"
        )
    shutil.rmtree(destination, ignore_errors=True)


def _expected_tarball_stem(package_name: str, version: str) -> str:
    return package_name.replace("@", "").replace("/", "-") + f"-{version}"


def _inspect_tarball(path: Path, *, expected_version: str) -> dict[str, Any]:
    with tarfile.open(path, "r:gz") as archive:
        package_member = archive.extractfile("package/package.json")
        if package_member is None:
            raise RuntimeError(f"{path} does not contain package/package.json")
        package_json = json.loads(package_member.read().decode("utf-8"))
    return {
        "path": str(path),
        "name": package_json.get("name"),
        "version": package_json.get("version"),
        "workspace_violations": _workspace_violations(path, package_json),
        "dev_version_literal_violations": _dev_version_literal_violations(
            path,
            expected_version=expected_version,
        ),
    }


def _workspace_violations(path: Path, package_json: dict[str, Any]) -> list[dict[str, str]]:
    violations: list[dict[str, str]] = []
    for section in DEPENDENCY_SECTIONS:
        deps = package_json.get(section)
        if not isinstance(deps, dict):
            continue
        for name, version in deps.items():
            if isinstance(version, str) and "workspace:" in version:
                violations.append(
                    {
                        "tarball": str(path),
                        "section": section,
                        "dependency": name,
                        "version": version,
                    }
                )
    return violations


def _dev_version_literal_violations(
    path: Path,
    *,
    expected_version: str,
) -> list[dict[str, str]]:
    violations: list[dict[str, str]] = []
    with tarfile.open(path, "r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            if not member.name.startswith(RUNTIME_MEMBER_PREFIXES):
                continue
            if not member.name.endswith(RUNTIME_MEMBER_SUFFIXES):
                continue
            extracted = archive.extractfile(member)
            if extracted is None:
                continue
            text = extracted.read().decode("utf-8", errors="ignore")
            if DEV_VERSION_LITERAL in text:
                violations.append(
                    {
                        "tarball": str(path),
                        "member": member.name,
                        "literal": DEV_VERSION_LITERAL,
                        "expected": expected_version,
                    }
                )
    return violations


def _summarize_command(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "args": result["args"],
        "returncode": result["returncode"],
        "stdout_tail": result["stdout_tail"],
        "stderr_tail": result["stderr_tail"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", help="Release tag, for example v0.1.0-alpha.1.")
    parser.add_argument(
        "--expected-version",
        help="Expected package version. Overrides --tag when provided.",
    )
    parser.add_argument("--package-manager", default="pnpm")
    parser.add_argument("--run-checks", action="store_true")
    parser.add_argument("--pack-destination", type=Path)
    parser.add_argument("--clean-pack-destination", action="store_true")
    parser.add_argument("--out-json", type=Path)
    args = parser.parse_args()

    root = repo_root()
    expected_version = expected_version_from_args(args.tag, args.expected_version)
    report = check_release_packages(
        root=root,
        expected_version=expected_version,
        package_manager=args.package_manager,
        run_checks=args.run_checks,
        pack_destination=args.pack_destination,
        clean_pack_destination=args.clean_pack_destination,
    )
    rendered = json.dumps(report, indent=2) + "\n"
    if args.out_json is not None:
        args.out_json.parent.mkdir(parents=True, exist_ok=True)
        args.out_json.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
