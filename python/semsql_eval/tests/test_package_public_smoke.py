from __future__ import annotations

from pathlib import Path
from typing import Any

from click.testing import CliRunner
from semsql_eval.__main__ import cli
from semsql_eval.package_public_smoke import (
    _parse_npm_view_version,
    _runtime_env,
    _semsql_dlx_command,
    _skipped_command,
    _version_command_matches,
    _view_packages_with_retries,
    render_package_public_smoke_markdown,
)
from semsql_eval.package_registry_smoke import PACKAGE_ORDER


def test_package_public_smoke_cli_help() -> None:
    result = CliRunner().invoke(cli, ["package-public-smoke", "--help"])
    assert result.exit_code == 0
    assert "--version" in result.output
    assert "--semsql-bin" in result.output
    assert "--manifest-url" in result.output
    assert "--timeout-seconds" in result.output
    assert "--package-check-retries" in result.output


def test_package_public_smoke_markdown_lists_package_versions() -> None:
    report = {
        "status": "pass",
        "registry_url": "https://registry.npmjs.org/",
        "version": "0.1.0-alpha.1",
        "native_binary_mode": "default_release_manifest",
        "artifacts": {"graph": "target/fixture/app.semsql"},
        "checks": {
            "package_versions_ok": True,
            "dlx_version_ok": True,
            "dlx_extract_ok": True,
            "dlx_query_ok": True,
            "dlx_extractor_help_ok": True,
            "dlx_extractor_version_ok": True,
        },
        "packages": [
            {
                "name": "@semsql/cli",
                "requested": "0.1.0-alpha.1",
                "resolved": "0.1.0-alpha.1",
                "returncode": 0,
            }
        ],
        "limits": ["requires published packages"],
    }
    rendered = render_package_public_smoke_markdown(report)
    assert "Package Public Smoke" in rendered
    assert "`package_versions_ok` | `PASS`" in rendered
    assert "`@semsql/cli` | `0.1.0-alpha.1` | `PASS`" in rendered
    assert "requires published packages" in rendered


def test_package_public_smoke_markdown_lists_failed_command_tails() -> None:
    report = {
        "status": "fail",
        "registry_url": "https://registry.npmjs.org/",
        "version": "0.1.0-alpha.1",
        "native_binary_mode": "default_release_manifest",
        "artifacts": {"graph": "target/fixture/app.semsql"},
        "checks": {"dlx_version_ok": False},
        "packages": [],
        "commands": {
            "dlx_version": {
                "args": ["pnpm", "--package", "@semsql/cli", "dlx", "semsql"],
                "returncode": 1,
                "stdout_tail": "",
                "stderr_tail": "download failed",
            }
        },
        "limits": [],
    }
    rendered = render_package_public_smoke_markdown(report)
    assert "Failed Commands" in rendered
    assert "`dlx_version`" in rendered
    assert "download failed" in rendered


def test_parse_npm_view_version_accepts_json_and_plain_text() -> None:
    assert _parse_npm_view_version('"0.1.0-alpha.1"\n') == "0.1.0-alpha.1"
    assert _parse_npm_view_version("0.1.0-alpha.1\n") == "0.1.0-alpha.1"
    assert _parse_npm_view_version("") is None


def test_version_command_matches_expected_native_version() -> None:
    assert _version_command_matches("semsql 0.1.0-alpha.1\n", "0.1.0-alpha.1")
    assert not _version_command_matches("semsql 0.1.0-dev\n", "0.1.0-alpha.1")
    assert not _version_command_matches("warning\nsemsql 0.1.0-alpha.1\n", "0.1.0-alpha.1")


def test_semsql_dlx_command_uses_explicit_package_form() -> None:
    assert _semsql_dlx_command("pnpm", "0.1.0-alpha.1", "--version") == [
        "pnpm",
        "--package",
        "@semsql/cli@0.1.0-alpha.1",
        "dlx",
        "semsql",
        "--version",
    ]


def test_skipped_command_records_reason() -> None:
    skipped = _skipped_command("missing package")
    assert skipped["returncode"] is None
    assert "missing package" in skipped["stderr_tail"]


def test_runtime_env_scrubs_ambient_launcher_overrides(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("SEMSQL_BIN", str(tmp_path / "local-semsql"))
    monkeypatch.setenv("SEMSQL_CLI_MANIFEST_URL", "file:///tmp/local-manifest.json")
    monkeypatch.setenv("SEMSQL_CLI_DOWNLOAD_BASE_URL", "file:///tmp/release")
    monkeypatch.setenv("SEMSQL_CLI_RELEASE_TAG", "vlocal")
    monkeypatch.setenv("SEMSQL_CLI_SKIP_DOWNLOAD", "1")
    monkeypatch.setenv("SEMSQL_CLI_VERSION", "0.1.0-dev")

    env = _runtime_env(
        registry_url="https://registry.npmjs.org/",
        cache_dir=tmp_path / "cache",
        semsql_bin=None,
        manifest_url=None,
    )

    assert "SEMSQL_BIN" not in env
    assert "SEMSQL_CLI_MANIFEST_URL" not in env
    assert "SEMSQL_CLI_DOWNLOAD_BASE_URL" not in env
    assert "SEMSQL_CLI_RELEASE_TAG" not in env
    assert "SEMSQL_CLI_SKIP_DOWNLOAD" not in env
    assert "SEMSQL_CLI_VERSION" not in env
    assert env["SEMSQL_CLI_CACHE_DIR"] == str(tmp_path / "cache")
    assert env["SEMSQL_EXTRACTOR_DISABLE_WORKSPACE"] == "1"


def test_runtime_env_allows_explicit_launcher_overrides(tmp_path: Path) -> None:
    binary = tmp_path / "semsql"
    manifest = "file:///tmp/manifest.json"

    env = _runtime_env(
        registry_url="https://registry.npmjs.org/",
        cache_dir=tmp_path / "cache",
        semsql_bin=binary,
        manifest_url=manifest,
    )

    assert env["SEMSQL_BIN"] == str(binary.resolve())
    assert env["SEMSQL_CLI_MANIFEST_URL"] == manifest


def test_view_packages_with_retries_records_attempts(monkeypatch: Any, tmp_path: Path) -> None:
    calls: list[str] = []

    def fake_run(args: list[str], **_kwargs: Any) -> dict[str, Any]:
        package = args[2].split("@0.1.0-alpha.1")[0]
        calls.append(package)
        ok = len(calls) > len(PACKAGE_ORDER)
        return {
            "args": args,
            "returncode": 0 if ok else 1,
            "stdout": '"0.1.0-alpha.1"' if ok else "",
            "stderr": "",
        }

    monkeypatch.setattr("semsql_eval.package_public_smoke._run", fake_run)
    views, commands = _view_packages_with_retries(
        npm="npm",
        version="0.1.0-alpha.1",
        registry_url="https://registry.npmjs.org/",
        cwd=tmp_path,
        timeout_seconds=1,
        retries=2,
        delay_seconds=0,
    )

    assert len(calls) == len(PACKAGE_ORDER) * 2
    assert all(item["resolved"] == "0.1.0-alpha.1" for item in views)
    assert f"view:{PACKAGE_ORDER[0]}:attempt1" in commands
    assert f"view:{PACKAGE_ORDER[-1]}:attempt2" in commands
