"""Public-registry smoke for published @semsql packages."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

from .package_registry_smoke import PACKAGE_ORDER


def run_package_public_smoke(
    *,
    out_dir: Path,
    version: str,
    semsql_bin: Path | None = None,
    manifest_url: str | None = None,
    repo_root: Path | None = None,
    package_manager: str = "pnpm",
    npm_bin: str = "npm",
    registry_url: str = "https://registry.npmjs.org/",
    timeout_seconds: int = 180,
    package_check_retries: int = 1,
    package_check_delay_seconds: float = 0.0,
) -> dict[str, Any]:
    root = repo_root or Path.cwd()
    pnpm = shutil.which(package_manager)
    if pnpm is None:
        raise RuntimeError(f"{package_manager} executable not found")
    npm = shutil.which(npm_bin)
    if npm is None:
        raise RuntimeError(f"{npm_bin} executable not found")
    if semsql_bin is not None and not semsql_bin.resolve().is_file():
        raise RuntimeError(f"native semsql binary does not exist: {semsql_bin}")

    out_dir.mkdir(parents=True, exist_ok=True)
    fixture_dir = _reset_child_dir(out_dir, "fixture")
    cache_dir = _reset_child_dir(out_dir, "binary-cache")

    package_views, commands = _view_packages_with_retries(
        npm=npm,
        version=version,
        registry_url=registry_url,
        cwd=root,
        timeout_seconds=timeout_seconds,
        retries=package_check_retries,
        delay_seconds=package_check_delay_seconds,
    )

    graph = fixture_dir / "app.semsql"
    package_versions_ok = all(
        item["returncode"] == 0 and item["resolved"] == version for item in package_views
    )
    if not package_versions_ok:
        skipped = _skipped_command("one or more @semsql packages are not published")
        commands.update(
            {
                "dlx_version": skipped,
                "extract": skipped,
                "query": skipped,
                "extractor_help": skipped,
                "extractor_version": skipped,
            }
        )
        checks = {
            "package_versions_ok": False,
            "dlx_version_ok": False,
            "dlx_extract_ok": False,
            "dlx_query_ok": False,
            "dlx_extractor_help_ok": False,
            "dlx_extractor_version_ok": False,
        }
        return _report(
            status="fail",
            version=version,
            registry_url=registry_url,
            package_manager=package_manager,
            native_binary_mode=_native_binary_mode(semsql_bin, manifest_url),
            out_dir=out_dir,
            fixture_dir=fixture_dir,
            graph=graph,
            cache_dir=cache_dir,
            package_views=package_views,
            checks=checks,
            commands=commands,
        )

    _write_laravel_fixture(fixture_dir)
    runtime_env = _runtime_env(
        registry_url=registry_url,
        cache_dir=cache_dir,
        semsql_bin=semsql_bin,
        manifest_url=manifest_url,
    )

    version_cmd = _run(
        [pnpm, "dlx", f"@semsql/cli@{version}", "semsql", "--version"],
        cwd=root,
        timeout_seconds=timeout_seconds,
        env=runtime_env,
        check=False,
    )
    extract = _run(
        [
            pnpm,
            "dlx",
            "--package",
            f"@semsql/cli@{version}",
            "--package",
            f"@semsql/extractor-cli@{version}",
            "semsql",
            "extract",
            str(fixture_dir),
            "--framework",
            "laravel",
            "--db-url",
            f"sqlite:{fixture_dir / 'app.sqlite'}",
            "--output",
            str(graph),
            "--no-sample-values",
        ],
        cwd=root,
        timeout_seconds=timeout_seconds,
        env=runtime_env,
        check=False,
    )
    query = _run(
        [
            pnpm,
            "dlx",
            "--package",
            f"@semsql/cli@{version}",
            "semsql",
            "query",
            "--graph",
            str(graph),
            "--dialect",
            "sqlite",
            "how many students",
        ],
        cwd=root,
        timeout_seconds=timeout_seconds,
        env=runtime_env,
        check=False,
    )
    extractor_help = _run(
        [
            pnpm,
            "dlx",
            "--package",
            f"@semsql/extractor-cli@{version}",
            "semsql-extract",
            "--help",
        ],
        cwd=root,
        timeout_seconds=timeout_seconds,
        env=_registry_env(registry_url),
        check=False,
    )
    extractor_version = _run(
        [
            pnpm,
            "dlx",
            "--package",
            f"@semsql/extractor-cli@{version}",
            "semsql-extract",
            "--version",
        ],
        cwd=root,
        timeout_seconds=timeout_seconds,
        env=_registry_env(registry_url),
        check=False,
    )
    commands.update(
        {
            "dlx_version": _summarize_command(version_cmd),
            "extract": _summarize_command(extract),
            "query": _summarize_command(query),
            "extractor_help": _summarize_command(extractor_help),
            "extractor_version": _summarize_command(extractor_version),
        }
    )

    query_first_line = query["stdout"].splitlines()[0] if query["stdout"] else ""
    checks = {
        "package_versions_ok": package_versions_ok,
        "dlx_version_ok": version_cmd["returncode"] == 0
        and _version_command_matches(version_cmd["stdout"], version),
        "dlx_extract_ok": extract["returncode"] == 0,
        "dlx_query_ok": query["returncode"] == 0
        and query_first_line == 'SELECT COUNT(*) FROM "users"',
        "dlx_extractor_help_ok": extractor_help["returncode"] == 0,
        "dlx_extractor_version_ok": extractor_version["returncode"] == 0
        and extractor_version["stdout"].strip() == version,
    }
    return _report(
        status="pass" if all(checks.values()) else "fail",
        version=version,
        registry_url=registry_url,
        package_manager=package_manager,
        native_binary_mode=_native_binary_mode(semsql_bin, manifest_url),
        out_dir=out_dir,
        fixture_dir=fixture_dir,
        graph=graph,
        cache_dir=cache_dir,
        package_views=package_views,
        checks=checks,
        commands=commands,
    )


def _report(
    *,
    status: str,
    version: str,
    registry_url: str,
    package_manager: str,
    native_binary_mode: str,
    out_dir: Path,
    fixture_dir: Path,
    graph: Path,
    cache_dir: Path,
    package_views: list[dict[str, Any]],
    checks: dict[str, bool],
    commands: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": status,
        "version": version,
        "registry_url": registry_url,
        "package_manager": package_manager,
        "native_binary_mode": native_binary_mode,
        "artifacts": {
            "out_dir": str(out_dir),
            "fixture_dir": str(fixture_dir),
            "graph": str(graph),
            "cache_dir": str(cache_dir),
        },
        "packages": package_views,
        "checks": checks,
        "commands": commands,
        "limits": [
            "Requires packages to already exist in the configured npm registry.",
            (
                "Ambient SEMSQL_BIN/SEMSQL_CLI_* overrides are scrubbed; "
                "explicit --semsql-bin or --manifest-url is diagnostic only."
            ),
        ],
    }


def _native_binary_mode(semsql_bin: Path | None, manifest_url: str | None) -> str:
    if semsql_bin is not None:
        return "override"
    if manifest_url is not None:
        return "manifest"
    return "default_release_manifest"


def render_package_public_smoke_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Package Public Smoke",
        "",
        f"- status: `{str(report['status']).upper()}`",
        f"- registry: `{report['registry_url']}`",
        f"- version: `{report['version']}`",
        f"- native binary mode: `{report['native_binary_mode']}`",
        f"- graph: `{report['artifacts']['graph']}`",
        "",
        "## Checks",
        "",
        "| Check | Result |",
        "|---|---:|",
    ]
    for key, value in report["checks"].items():
        lines.append(f"| `{key}` | `{'PASS' if value else 'FAIL'}` |")
    lines.extend(["", "## Packages", "", "| Package | Version | Result |", "|---|---:|---:|"])
    for item in report["packages"]:
        result = "PASS" if item["returncode"] == 0 and item["resolved"] == item["requested"] else "FAIL"
        lines.append(f"| `{item['name']}` | `{item['resolved'] or ''}` | `{result}` |")
    lines.extend(["", "## Limits", ""])
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


def _view_packages_with_retries(
    *,
    npm: str,
    version: str,
    registry_url: str,
    cwd: Path,
    timeout_seconds: int,
    retries: int,
    delay_seconds: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    attempts = max(retries, 1)
    commands: dict[str, Any] = {}
    latest: list[dict[str, Any]] = []
    for attempt in range(1, attempts + 1):
        latest = []
        for package in PACKAGE_ORDER:
            view = _run(
                [
                    npm,
                    "view",
                    f"{package}@{version}",
                    "version",
                    "--registry",
                    registry_url,
                    "--json",
                ],
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                env=_registry_env(registry_url),
                check=False,
            )
            commands[f"view:{package}:attempt{attempt}"] = _summarize_command(view)
            latest.append(
                {
                    "name": package,
                    "requested": version,
                    "returncode": view["returncode"],
                    "resolved": _parse_npm_view_version(view["stdout"]),
                }
            )
        if all(
            item["returncode"] == 0 and item["resolved"] == version for item in latest
        ):
            break
        if attempt < attempts and delay_seconds > 0:
            time.sleep(delay_seconds)
    return latest, commands


def _write_laravel_fixture(root: Path) -> None:
    (root / "composer.json").write_text(
        json.dumps({"require": {"laravel/framework": "^11.0"}}),
        encoding="utf-8",
    )
    resource = root / "app" / "Filament" / "Resources" / "StudentResource.php"
    resource.parent.mkdir(parents=True, exist_ok=True)
    resource.write_text(
        """<?php
namespace App\\Filament\\Resources;
class StudentResource extends Resource {
    protected static ?string $model = User::class;
    protected static ?string $modelLabel = 'Student';
    protected static ?string $pluralModelLabel = 'Students';
}
""",
        encoding="utf-8",
    )
    conn = sqlite3.connect(root / "app.sqlite")
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            email TEXT,
            status TEXT,
            is_active INTEGER
        );
        INSERT INTO users (id, email, status, is_active)
        VALUES (1, 'one@example.test', 'active', 1);
        """
    )
    conn.close()


def _runtime_env(
    *,
    registry_url: str,
    cache_dir: Path,
    semsql_bin: Path | None,
    manifest_url: str | None,
) -> dict[str, str]:
    env = _registry_env(registry_url)
    _scrub_launcher_overrides(env)
    env["SEMSQL_EXTRACTOR_DISABLE_WORKSPACE"] = "1"
    env["SEMSQL_CLI_CACHE_DIR"] = str(cache_dir)
    if semsql_bin is not None:
        env["SEMSQL_BIN"] = str(semsql_bin.resolve())
    if manifest_url is not None:
        env["SEMSQL_CLI_MANIFEST_URL"] = manifest_url
    return env


def _scrub_launcher_overrides(env: dict[str, str]) -> None:
    for key in list(env):
        if key == "SEMSQL_BIN" or key.startswith("SEMSQL_CLI_"):
            del env[key]


def _registry_env(registry_url: str) -> dict[str, str]:
    env = os.environ.copy()
    env["NPM_CONFIG_REGISTRY"] = registry_url
    env["npm_config_registry"] = registry_url
    return env


def _parse_npm_view_version(stdout: str) -> str | None:
    text = stdout.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text.strip('"')
    return parsed if isinstance(parsed, str) else None


def _version_command_matches(stdout: str, version: str) -> bool:
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    return bool(lines) and lines[0] == f"semsql {version}"


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


def _skipped_command(reason: str) -> dict[str, Any]:
    return {
        "args": [],
        "returncode": None,
        "stdout_tail": "",
        "stderr_tail": f"skipped: {reason}",
    }
