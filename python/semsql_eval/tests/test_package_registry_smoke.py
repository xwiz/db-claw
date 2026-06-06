from __future__ import annotations

from click.testing import CliRunner
from semsql_eval.__main__ import cli
from semsql_eval.package_registry_smoke import render_package_registry_smoke_markdown


def test_package_registry_smoke_cli_help() -> None:
    result = CliRunner().invoke(cli, ["package-registry-smoke", "--help"])
    assert result.exit_code == 0
    assert "--semsql-bin" in result.output
    assert "--version" in result.output


def test_package_registry_smoke_markdown_lists_full_stack_read() -> None:
    report = {
        "status": "pass",
        "registry_url": "http://127.0.0.1:4873",
        "version": "0.1.0-dev",
        "published": [{"name": "@semsql/cli"}],
        "artifacts": {"graph": "target/fixture/app.semsql"},
        "checks": {
            "pack_ok": True,
            "registry_started": True,
            "publish_ok": True,
            "dlx_extract_ok": True,
            "dlx_query_ok": True,
            "dlx_extractor_help_ok": True,
            "dlx_extractor_version_ok": True,
        },
        "limits": ["local registry only"],
    }
    rendered = render_package_registry_smoke_markdown(report)
    assert "Package Registry Smoke" in rendered
    assert "`dlx_extract_ok` | `PASS`" in rendered
    assert "full local-registry npm path" in rendered
