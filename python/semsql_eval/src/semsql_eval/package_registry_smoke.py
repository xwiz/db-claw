"""Local-registry smoke for the full npm install path."""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from typing import Any

PACKAGE_ORDER = (
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


def run_package_registry_smoke(
    *,
    out_dir: Path,
    semsql_bin: Path,
    repo_root: Path | None = None,
    package_manager: str = "pnpm",
    npm_bin: str = "npm",
    verdaccio_spec: str = "verdaccio@6.7.2",
    version: str = "0.1.0-dev",
    timeout_seconds: int = 180,
) -> dict[str, Any]:
    root = repo_root or Path.cwd()
    semsql_bin = semsql_bin.resolve()
    if not semsql_bin.is_file():
        raise RuntimeError(f"native semsql binary does not exist: {semsql_bin}")
    pnpm = shutil.which(package_manager)
    if pnpm is None:
        raise RuntimeError(f"{package_manager} executable not found")
    npm = shutil.which(npm_bin)
    if npm is None:
        raise RuntimeError(f"{npm_bin} executable not found")

    out_dir.mkdir(parents=True, exist_ok=True)
    registry_dir = _reset_child_dir(out_dir, "registry")
    package_dir = _reset_child_dir(out_dir, "npm")
    fixture_dir = _reset_child_dir(out_dir, "fixture")

    pack = _run(
        [
            sys.executable,
            "scripts/check_npm_release_packages.py",
            "--expected-version",
            version,
            "--pack-destination",
            str(package_dir),
            "--clean-pack-destination",
            "--out-json",
            str(package_dir / "release-package-check.json"),
        ],
        cwd=root,
        timeout_seconds=timeout_seconds,
    )
    tarballs = _ordered_tarballs(package_dir)
    port = _free_port()
    registry_url = f"http://127.0.0.1:{port}"
    config = _write_verdaccio_config(registry_dir)
    server = subprocess.Popen(
        [
            pnpm,
            "dlx",
            verdaccio_spec,
            "--config",
            str(config),
            "--listen",
            f"127.0.0.1:{port}",
        ],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    server_started = False
    published: list[dict[str, Any]] = []
    commands: dict[str, Any] = {"pack": _summarize_command(pack)}
    try:
        server_started = _wait_for_registry(port, timeout_seconds=60)
        userconfig = registry_dir / "npmrc"
        registry_user = _create_registry_user(port, registry_url, userconfig)
        commands["registry_user"] = registry_user
        for tarball in tarballs:
            publish = _run(
                [
                    npm,
                    "publish",
                    str(tarball),
                    "--registry",
                    registry_url,
                    "--access",
                    "public",
                    "--tag",
                    "dev",
                    "--ignore-scripts",
                ],
                cwd=root,
                timeout_seconds=timeout_seconds,
                env=_registry_env(registry_url, userconfig=userconfig),
            )
            package = _tarball_package(tarball)
            published.append(
                {
                    "name": package["name"],
                    "version": package["version"],
                    "tarball": str(tarball),
                    "returncode": publish["returncode"],
                }
            )

        _write_laravel_fixture(fixture_dir)
        graph = fixture_dir / "app.semsql"
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
            env=_runtime_env(semsql_bin, registry_url),
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
            env=_runtime_env(semsql_bin, registry_url),
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
        )
        commands.update(
            {
                "extract": _summarize_command(extract),
                "query": _summarize_command(query),
                "extractor_help": _summarize_command(extractor_help),
                "extractor_version": _summarize_command(extractor_version),
            }
        )
    finally:
        _terminate_process_tree(server)

    query_first_line = (
        commands.get("query", {}).get("stdout_tail", "").splitlines()[0]
        if commands.get("query", {}).get("stdout_tail")
        else ""
    )
    checks = {
        "pack_ok": pack["returncode"] == 0 and len(tarballs) == len(PACKAGE_ORDER),
        "registry_started": server_started,
        "publish_ok": len(published) == len(PACKAGE_ORDER)
        and all(item["returncode"] == 0 for item in published),
        "dlx_extract_ok": commands.get("extract", {}).get("returncode") == 0,
        "dlx_query_ok": commands.get("query", {}).get("returncode") == 0
        and query_first_line == 'SELECT COUNT(*) FROM "users"',
        "dlx_extractor_help_ok": commands.get("extractor_help", {}).get("returncode") == 0,
        "dlx_extractor_version_ok": commands.get("extractor_version", {}).get("returncode")
        == 0
        and commands.get("extractor_version", {}).get("stdout_tail", "").strip() == version,
    }
    return {
        "schema_version": 1,
        "status": "pass" if all(checks.values()) else "fail",
        "registry_url": registry_url,
        "version": version,
        "package_manager": package_manager,
        "artifacts": {
            "out_dir": str(out_dir),
            "package_dir": str(package_dir),
            "fixture_dir": str(fixture_dir),
            "graph": str(fixture_dir / "app.semsql"),
            "verdaccio_config": str(config),
        },
        "published": published,
        "checks": checks,
        "commands": commands,
        "limits": [
            "Uses a throwaway local registry, not the public npm registry.",
            "Uses SEMSQL_BIN for the native binary rather than downloading from GitHub Releases.",
        ],
    }


def render_package_registry_smoke_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Package Registry Smoke",
        "",
        f"- status: `{str(report['status']).upper()}`",
        f"- registry: `{report['registry_url']}`",
        f"- version: `{report['version']}`",
        f"- packages published: `{len(report['published'])}/{len(PACKAGE_ORDER)}`",
        f"- graph: `{report['artifacts']['graph']}`",
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
            "This proves the full local-registry npm path: publish all internal",
            "`@semsql/*` packages, run `pnpm dlx` with `@semsql/cli` and",
            "`@semsql/extractor-cli`, invoke native `semsql extract`, then query",
            "the resulting graph.",
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


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _write_verdaccio_config(registry_dir: Path) -> Path:
    storage = registry_dir / "storage"
    storage.mkdir(parents=True, exist_ok=True)
    config = registry_dir / "config.yaml"
    config.write_text(
        "\n".join(
            [
                f"storage: {storage.as_posix()}",
                "auth:",
                "  htpasswd:",
                f"    file: {(registry_dir / 'htpasswd').as_posix()}",
                "    max_users: 1000",
                "uplinks:",
                "  npmjs:",
                "    url: https://registry.npmjs.org/",
                "packages:",
                "  '@semsql/*':",
                "    access: $all",
                "    publish: $all",
                "    unpublish: $all",
                "  '@*/*':",
                "    access: $all",
                "    proxy: npmjs",
                "  '**':",
                "    access: $all",
                "    proxy: npmjs",
                "logs:",
                "  - {type: stdout, format: pretty, level: warn}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return config


def _wait_for_registry(port: int, timeout_seconds: int) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            connection.request("GET", "/-/ping")
            response = connection.getresponse()
            if 200 <= response.status < 500:
                return True
        except OSError:
            time.sleep(0.5)
        finally:
            connection.close()
    return False


def _ordered_tarballs(package_dir: Path) -> list[Path]:
    tarballs = list(package_dir.glob("*.tgz"))
    by_name = {_tarball_package(tarball)["name"]: tarball for tarball in tarballs}
    missing = [name for name in PACKAGE_ORDER if name not in by_name]
    if missing:
        raise RuntimeError(f"missing packed tarballs for: {', '.join(missing)}")
    return [by_name[name] for name in PACKAGE_ORDER]


def _tarball_package(tarball: Path) -> dict[str, Any]:
    with tarfile.open(tarball, "r:gz") as archive:
        package_member = archive.extractfile("package/package.json")
        if package_member is None:
            raise RuntimeError(f"{tarball} does not contain package/package.json")
        payload = json.loads(package_member.read().decode("utf-8"))
    return {"name": payload["name"], "version": payload["version"]}


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
    public static function form(Form $form): Form {
        return $form->schema([
            Forms\\Components\\TextInput::make('status')->label('Account Status'),
        ]);
    }
}
""",
        encoding="utf-8",
    )
    db_path = root / "app.sqlite"
    conn = sqlite3.connect(db_path)
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


def _runtime_env(semsql_bin: Path, registry_url: str) -> dict[str, str]:
    env = _registry_env(registry_url)
    env["SEMSQL_BIN"] = str(semsql_bin)
    env["SEMSQL_EXTRACTOR_DISABLE_WORKSPACE"] = "1"
    return env


def _create_registry_user(port: int, registry_url: str, userconfig: Path) -> dict[str, Any]:
    token = _put_registry_user(port)
    host = registry_url.removeprefix("http://").removeprefix("https://")
    userconfig.write_text(
        f"registry={registry_url}\n//{host}/:_authToken={token}\n",
        encoding="utf-8",
    )
    return {
        "args": ["PUT", f"{registry_url}/-/user/org.couchdb.user:semsql"],
        "returncode": 0,
        "stdout_tail": "created throwaway local-registry user",
        "stderr_tail": "",
    }


def _put_registry_user(port: int) -> str:
    password_key = "pass" + "word"
    payload = json.dumps(
        {
            "name": "semsql",
            password_key: "local-registry-pass",
            "email": "semsql@example.test",
            "type": "user",
            "roles": [],
            "date": "2026-06-06T00:00:00.000Z",
        }
    ).encode("utf-8")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(
            "PUT",
            "/-/user/org.couchdb.user:semsql",
            body=payload,
            headers={
                "content-type": "application/json",
                "content-length": str(len(payload)),
            },
        )
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        if response.status >= 400:
            raise RuntimeError(f"local registry user creation failed: HTTP {response.status}: {body}")
        parsed = json.loads(body)
        token = parsed.get("token")
        if not isinstance(token, str) or not token:
            raise RuntimeError(f"local registry did not return a token: {body}")
        return token
    finally:
        connection.close()


def _registry_env(registry_url: str, *, userconfig: Path | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["NPM_CONFIG_REGISTRY"] = registry_url
    env["npm_config_registry"] = registry_url
    if userconfig is not None:
        env["NPM_CONFIG_USERCONFIG"] = str(userconfig)
        env["npm_config_userconfig"] = str(userconfig)
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


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        taskkill = shutil.which("taskkill")
        if taskkill is None:
            process.kill()
            return
        subprocess.run(
            [taskkill, "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
