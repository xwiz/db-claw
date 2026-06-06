from __future__ import annotations

from click.testing import CliRunner
from semsql_eval.__main__ import cli
from semsql_eval.package_dlx_smoke import render_package_dlx_smoke_markdown


def test_package_dlx_smoke_cli_help() -> None:
    result = CliRunner().invoke(cli, ["package-dlx-smoke", "--help"])
    assert result.exit_code == 0
    assert "--semsql-bin" in result.output
    assert "--package-dir" in result.output


def test_package_dlx_smoke_markdown_lists_limits() -> None:
    report = {
        "status": "pass",
        "package": "@semsql/cli",
        "package_manager": "pnpm",
        "target": {"key": "win32-x64"},
        "artifacts": {
            "tarball": "target/pkg/semsql-cli.tgz",
            "cached_binary": "cache/semsql.exe",
        },
        "checks": {
            "pack_ok": True,
            "dlx_semsql_bin_override_ok": True,
            "dlx_manifest_download_ok": True,
            "dlx_skip_download_fails_closed": True,
        },
        "limits": ["registry proof still needed"],
    }
    rendered = render_package_dlx_smoke_markdown(report)
    assert "Package dlx Smoke" in rendered
    assert "`dlx_manifest_download_ok` | `PASS`" in rendered
    assert "registry proof still needed" in rendered
