#!/usr/bin/env python3
"""Publish packed @semsql npm release tarballs in dependency order."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Any

RELEASE_PACKAGE_ORDER = (
    "@semsql/extractor-sdk",
    "@semsql/extractor-i18n",
    "@semsql/extractor-django",
    "@semsql/extractor-laravel",
    "@semsql/extractor-nextjs",
    "@semsql/extractor-rails",
    "@semsql/extractor-vue",
    "@semsql/extractor-cli",
    "@semsql/cli",
)


def expected_version_from_args(tag: str | None, expected_version: str | None) -> str:
    if expected_version:
        return expected_version
    if not tag:
        raise ValueError("pass --tag or --expected-version")
    version = tag.removeprefix("v")
    if not version:
        raise ValueError("--tag must include a version")
    return version


def default_dist_tag(version: str) -> str:
    return "next" if "-" in version else "latest"


def publish_release_packages(
    *,
    package_dir: Path,
    expected_version: str,
    npm_bin: str = "npm",
    registry_url: str = "https://registry.npmjs.org/",
    dist_tag: str | None = None,
    provenance: bool = False,
    skip_existing: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    npm = shutil.which(npm_bin)
    if npm is None:
        raise RuntimeError(f"{npm_bin} executable not found")
    package_dir = package_dir.resolve()
    tarballs = _ordered_tarballs(package_dir, expected_version)
    tag = dist_tag or default_dist_tag(expected_version)
    auth = None if dry_run else _npm_auth_report(npm, package_dir, registry_url)
    if auth is not None and auth["returncode"] != 0:
        return {
            "schema_version": 1,
            "status": "fail",
            "expected_version": expected_version,
            "registry_url": registry_url,
            "dist_tag": tag,
            "provenance": provenance,
            "skip_existing": skip_existing,
            "dry_run": dry_run,
            "auth": auth,
            "package_count": len(tarballs),
            "results": [],
            "failures": [
                {
                    "kind": "npm_auth",
                    "message": "npm whoami failed; check NPM_TOKEN permissions.",
                    "auth": auth,
                }
            ],
            "diagnosis": "npm_auth_failed",
        }
    results: list[dict[str, Any]] = []
    for package in tarballs:
        existing: dict[str, Any] | None = None
        if skip_existing:
            view = _run(
                [
                    npm,
                    "view",
                    f"{package['name']}@{expected_version}",
                    "version",
                    "--registry",
                    registry_url,
                    "--json",
                ],
                cwd=package_dir,
                check=False,
            )
            existing = {
                "returncode": view["returncode"],
                "resolved": _parse_npm_view_version(view["stdout"]),
                "command": _summarize_command(view),
            }
            if existing["resolved"] == expected_version:
                results.append(
                    {
                        "name": package["name"],
                        "version": package["version"],
                        "tarball": str(package["path"]),
                        "action": "skip-existing",
                        "existing": existing,
                        "publish": None,
                        "ok": True,
                    }
                )
                continue

        command = [
            npm,
            "publish",
            str(package["path"]),
            "--registry",
            registry_url,
            "--access",
            "public",
            "--tag",
            tag,
        ]
        if provenance:
            command.append("--provenance")
        if dry_run:
            publish = {
                "args": command,
                "returncode": 0,
                "stdout": "dry run: publish not executed",
                "stderr": "",
            }
            action = "dry-run"
        else:
            publish = _run(command, cwd=package_dir, check=False)
            action = "publish"
        results.append(
            {
                "name": package["name"],
                "version": package["version"],
                "tarball": str(package["path"]),
                "action": action,
                "existing": existing,
                "publish": _summarize_command(publish),
                "ok": publish["returncode"] == 0,
            }
        )

    failures = [item for item in results if not item["ok"]]
    return {
        "schema_version": 1,
        "status": "pass" if not failures else "fail",
        "expected_version": expected_version,
        "registry_url": registry_url,
        "dist_tag": tag,
        "provenance": provenance,
        "skip_existing": skip_existing,
        "dry_run": dry_run,
        "auth": auth,
        "package_count": len(tarballs),
        "results": results,
        "failures": failures,
        "diagnosis": _diagnose_failures(failures),
    }


def _ordered_tarballs(package_dir: Path, expected_version: str) -> list[dict[str, Any]]:
    tarballs = list(package_dir.glob("*.tgz"))
    by_name: dict[str, dict[str, Any]] = {}
    for tarball in tarballs:
        package = _tarball_package(tarball)
        if package["version"] != expected_version:
            continue
        by_name[package["name"]] = package
    missing = [name for name in RELEASE_PACKAGE_ORDER if name not in by_name]
    if missing:
        raise RuntimeError(f"missing tarballs for: {', '.join(missing)}")
    return [by_name[name] for name in RELEASE_PACKAGE_ORDER]


def _tarball_package(tarball: Path) -> dict[str, Any]:
    with tarfile.open(tarball, "r:gz") as archive:
        package_member = archive.extractfile("package/package.json")
        if package_member is None:
            raise RuntimeError(f"{tarball} does not contain package/package.json")
        payload = json.loads(package_member.read().decode("utf-8"))
    return {
        "path": tarball,
        "name": payload["name"],
        "version": payload["version"],
    }


def _parse_npm_view_version(stdout: str) -> str | None:
    text = stdout.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text.strip('"')
    return parsed if isinstance(parsed, str) else None


def _npm_auth_report(npm: str, package_dir: Path, registry_url: str) -> dict[str, Any]:
    whoami = _run(
        [npm, "whoami", "--registry", registry_url],
        cwd=package_dir,
        check=False,
    )
    return {
        "returncode": whoami["returncode"],
        "username": _parse_npm_whoami(whoami["stdout"]),
        "command": _summarize_command(whoami),
    }


def _parse_npm_whoami(stdout: str) -> str | None:
    username = stdout.strip().strip('"')
    return username or None


def _diagnose_failures(failures: list[dict[str, Any]]) -> str | None:
    if not failures:
        return None
    publish_tails = [
        str(item.get("publish", {}).get("stderr_tail", ""))
        for item in failures
        if isinstance(item.get("publish"), dict)
    ]
    if publish_tails and all(
        "npm error code E404" in tail and "Not Found - PUT" in tail
        for tail in publish_tails
    ):
        return "npm_scope_permission_or_missing_org"
    return "npm_publish_failed"


def _run(args: list[str], *, cwd: Path, check: bool = True) -> dict[str, Any]:
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)
    result = {
        "args": args,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"command failed with exit {proc.returncode}: {' '.join(args)}\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    return result


def _summarize_command(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "args": result["args"],
        "returncode": result["returncode"],
        "stdout_tail": str(result.get("stdout", ""))[-2000:],
        "stderr_tail": str(result.get("stderr", ""))[-2000:],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", help="Release tag, for example v0.1.0-alpha.1.")
    parser.add_argument("--expected-version", help="Expected package version.")
    parser.add_argument("--package-dir", type=Path, required=True)
    parser.add_argument("--npm-bin", default="npm")
    parser.add_argument("--registry-url", default="https://registry.npmjs.org/")
    parser.add_argument("--dist-tag", help="npm dist-tag. Defaults to next for prereleases.")
    parser.add_argument("--provenance", action="store_true")
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--out-json", type=Path)
    args = parser.parse_args()

    expected_version = expected_version_from_args(args.tag, args.expected_version)
    report = publish_release_packages(
        package_dir=args.package_dir,
        expected_version=expected_version,
        npm_bin=args.npm_bin,
        registry_url=args.registry_url,
        dist_tag=args.dist_tag,
        provenance=args.provenance,
        skip_existing=args.skip_existing,
        dry_run=args.dry_run,
    )
    rendered = json.dumps(report, indent=2) + "\n"
    if args.out_json is not None:
        args.out_json.parent.mkdir(parents=True, exist_ok=True)
        args.out_json.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
