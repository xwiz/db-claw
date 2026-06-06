"""pnpm dlx smoke for the npm semsql launcher."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from urllib.request import pathname2url


@dataclass(frozen=True)
class DlxTarget:
    key: str
    binary_name: str


def current_dlx_target() -> DlxTarget:
    machine = platform.machine().lower()
    if machine in {"amd64", "x64", "x86_64"}:
        arch = "x64"
    elif machine in {"aarch64", "arm64"}:
        arch = "arm64"
    else:
        raise RuntimeError(f"unsupported dlx smoke architecture: {platform.machine()}")
    if sys.platform.startswith("win"):
        return DlxTarget(f"win32-{arch}", "semsql.exe")
    if sys.platform == "darwin":
        return DlxTarget(f"darwin-{arch}", "semsql")
    if sys.platform.startswith("linux"):
        return DlxTarget(f"linux-{arch}", "semsql")
    raise RuntimeError(f"unsupported dlx smoke platform: {sys.platform}")


def run_package_dlx_smoke(
    *,
    out_dir: Path,
    semsql_bin: Path,
    package_dir: Path | None = None,
    repo_root: Path | None = None,
    package_manager: str = "pnpm",
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    root = repo_root or Path.cwd()
    package_root = (package_dir or root / "packages" / "semsql-cli").resolve()
    semsql_bin = semsql_bin.resolve()
    if not package_root.is_dir():
        raise RuntimeError(f"@semsql/cli package dir does not exist: {package_root}")
    if not semsql_bin.is_file():
        raise RuntimeError(f"native semsql binary does not exist: {semsql_bin}")
    tool = shutil.which(package_manager)
    if tool is None:
        raise RuntimeError(f"{package_manager} executable not found")

    out_dir.mkdir(parents=True, exist_ok=True)
    npm_dir = _reset_child_dir(out_dir, "npm")
    cache_dir = _reset_child_dir(out_dir, "binary-cache")
    manifest_dir = _reset_child_dir(out_dir, "manifest")

    pack = _run(
        [tool, "--dir", str(package_root), "pack", "--pack-destination", str(npm_dir)],
        cwd=root,
        timeout_seconds=timeout_seconds,
    )
    tarballs = sorted(npm_dir.glob("*.tgz"))
    if len(tarballs) != 1:
        raise RuntimeError(f"expected exactly one semsql-cli tarball, found {len(tarballs)}")
    tarball = tarballs[0]
    tarball_spec = f"file:{tarball}"

    override = _run(
        [tool, "dlx", "--package", tarball_spec, "semsql", "--version"],
        cwd=root,
        env=_launcher_env({"SEMSQL_BIN": str(semsql_bin)}),
        timeout_seconds=timeout_seconds,
    )

    target = current_dlx_target()
    manifest_path = _write_local_manifest(
        manifest_dir=manifest_dir,
        semsql_bin=semsql_bin,
        target=target,
    )
    manifest = _run(
        [tool, "dlx", "--package", tarball_spec, "semsql", "--version"],
        cwd=root,
        env=_launcher_env(
            {
                "SEMSQL_CLI_VERSION": "0.1.0-local-dlx-smoke",
                "SEMSQL_CLI_CACHE_DIR": str(cache_dir),
                "SEMSQL_CLI_MANIFEST_URL": _file_url(manifest_path),
            }
        ),
        timeout_seconds=timeout_seconds,
    )
    cached_binary = cache_dir / "0.1.0-local-dlx-smoke" / target.key / target.binary_name

    skip_download = _run(
        [tool, "dlx", "--package", tarball_spec, "semsql", "--version"],
        cwd=root,
        env=_launcher_env(
            {
                "SEMSQL_CLI_VERSION": "0.1.0-local-dlx-skip",
                "SEMSQL_CLI_CACHE_DIR": str(out_dir / "empty-cache"),
                "SEMSQL_CLI_SKIP_DOWNLOAD": "1",
            }
        ),
        timeout_seconds=timeout_seconds,
        check=False,
    )

    checks = {
        "pack_ok": pack["returncode"] == 0 and tarball.is_file(),
        "dlx_semsql_bin_override_ok": override["returncode"] == 0,
        "dlx_manifest_download_ok": manifest["returncode"] == 0 and cached_binary.is_file(),
        "dlx_skip_download_fails_closed": (
            skip_download["returncode"] != 0
            and "not cached" in (skip_download["stderr"] + skip_download["stdout"])
        ),
    }
    return {
        "schema_version": 1,
        "status": "pass" if all(checks.values()) else "fail",
        "package": "@semsql/cli",
        "package_manager": package_manager,
        "target": {"key": target.key, "binary_name": target.binary_name},
        "artifacts": {
            "out_dir": str(out_dir),
            "tarball": str(tarball),
            "manifest": str(manifest_path),
            "cached_binary": str(cached_binary),
        },
        "checks": checks,
        "commands": {
            "pack": _summarize_command(pack),
            "dlx_semsql_bin_override": _summarize_command(override),
            "dlx_manifest_download": _summarize_command(manifest),
            "dlx_skip_download": _summarize_command(skip_download),
        },
        "limits": [
            "Uses a local tarball and local file manifest, not the public npm registry.",
            "Extractor-stack dlx still requires all @semsql/* packages to be published.",
        ],
    }


def render_package_dlx_smoke_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Package dlx Smoke",
        "",
        f"- status: `{str(report['status']).upper()}`",
        f"- package: `{report['package']}`",
        f"- package manager: `{report['package_manager']}`",
        f"- target: `{report['target']['key']}`",
        f"- tarball: `{report['artifacts']['tarball']}`",
        f"- cached binary: `{report['artifacts']['cached_binary']}`",
        "",
        "## Checks",
        "",
        "| Check | Result |",
        "|---|---:|",
    ]
    for key, value in report["checks"].items():
        lines.append(f"| `{key}` | `{'PASS' if value else 'FAIL'}` |")
    lines.extend(
        [
            "",
            "## Read",
            "",
            "`pnpm dlx --package <local @semsql/cli tarball> semsql --version`",
            "works with `SEMSQL_BIN`, works with manifest download/cache, and",
            "fails closed when downloads are disabled.",
            "",
            "## Limits",
            "",
        ]
    )
    for item in report["limits"]:
        lines.append(f"- {item}")
    return "\n".join(lines) + "\n"


def _reset_child_dir(parent: Path, child_name: str) -> Path:
    path = parent / child_name
    resolved_parent = parent.resolve()
    resolved_path = path.resolve()
    if resolved_parent != resolved_path and resolved_parent not in resolved_path.parents:
        raise RuntimeError(f"refusing to reset directory outside output root: {path}")
    shutil.rmtree(resolved_path, ignore_errors=True)
    resolved_path.mkdir(parents=True, exist_ok=True)
    return resolved_path


def _launcher_env(extra: dict[str, str]) -> dict[str, str]:
    env = os.environ.copy()
    for key in list(env):
        if key == "SEMSQL_BIN" or key.startswith("SEMSQL_CLI_"):
            env.pop(key, None)
    env.update(extra)
    return env


def _run(
    args: list[str],
    *,
    cwd: Path,
    timeout_seconds: int,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> dict[str, Any]:
    proc = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
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


def _write_local_manifest(
    *,
    manifest_dir: Path,
    semsql_bin: Path,
    target: DlxTarget,
) -> Path:
    body = semsql_bin.read_bytes()
    manifest = {
        "version": "0.1.0-local-dlx-smoke",
        "assets": {
            target.key: {
                "url": _file_url(semsql_bin),
                "sha256": hashlib.sha256(body).hexdigest(),
                "size": len(body),
            }
        },
    }
    manifest_path = manifest_dir / "semsql-downloads.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest_path


def _file_url(path: Path) -> str:
    return urljoin("file:", pathname2url(str(path.resolve())))
