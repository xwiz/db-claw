from __future__ import annotations

from click.testing import CliRunner
from semsql_eval.__main__ import cli
from semsql_eval.package_launcher_smoke import (
    node_target_for,
    render_package_launcher_smoke_markdown,
)


def test_package_launcher_smoke_cli_help() -> None:
    result = CliRunner().invoke(cli, ["package-launcher-smoke", "--help"])
    assert result.exit_code == 0
    assert "--semsql-bin" in result.output
    assert "--package-dir" in result.output


def test_node_target_for_supported_platforms() -> None:
    assert node_target_for("win32", "AMD64").key == "win32-x64"
    assert node_target_for("win32", "AMD64").binary_name == "semsql.exe"
    assert node_target_for("linux", "x86_64").key == "linux-x64"
    assert node_target_for("darwin", "arm64").key == "darwin-arm64"


def test_package_launcher_smoke_markdown_lists_checks() -> None:
    report = {
        "status": "pass",
        "package": "@semsql/cli",
        "package_manager": "pnpm",
        "target": {"key": "win32-x64"},
        "artifacts": {
            "tarball": "target/pkg/semsql-cli.tgz",
            "installed_bin": "node_modules/.bin/semsql.cmd",
            "cached_binary": "cache/semsql.exe",
        },
        "checks": {
            "pack_ok": True,
            "install_ok": True,
            "bin_link_ok": True,
            "semsql_bin_override_ok": True,
            "manifest_download_ok": True,
            "skip_download_fails_closed": True,
        },
        "limits": ["local tarball only"],
    }
    rendered = render_package_launcher_smoke_markdown(report)
    assert "Package Launcher Smoke" in rendered
    assert "`manifest_download_ok` | `PASS`" in rendered
    assert "real tagged-release `pnpm dlx` proof" in rendered
