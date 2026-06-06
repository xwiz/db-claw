"""Installed-style package bridge probe.

This verifies that the native ``semsql`` binary can find ``semsql-extract`` via
PATH, the way it must after ``@semsql/cli`` and ``@semsql/extractor-cli`` are
installed together. The probe disables workspace fallback so a local checkout
cannot mask a packaging failure.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

from .framework_bridge_probe import (
    render_framework_bridge_probe_markdown,
    run_framework_bridge_probe,
)


def run_package_bridge_probe(
    *,
    out_dir: Path,
    semsql_bin: Path,
    build_extractor: bool = False,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    root = repo_root or Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)
    if build_extractor:
        pnpm = shutil.which("pnpm")
        if pnpm is None:
            raise RuntimeError("pnpm executable not found; cannot build extractor CLI")
        subprocess.run(
            [pnpm, "--filter", "@semsql/extractor-cli", "build"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )

    extractor_script = root / "packages" / "extractor-cli" / "dist" / "cli.js"
    if not extractor_script.is_file():
        raise RuntimeError(
            f"extractor CLI is not built at {extractor_script}; pass --build-extractor"
        )

    bin_dir = out_dir / "installed-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim_path = _write_extractor_shim(bin_dir, extractor_script)
    env = os.environ.copy()
    env.pop("SEMSQL_EXTRACTOR_BIN", None)
    env["SEMSQL_EXTRACTOR_DISABLE_WORKSPACE"] = "1"
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"

    bridge_report = run_framework_bridge_probe(
        out_dir=out_dir / "framework-fixtures",
        semsql_bin=semsql_bin,
        build_extractor=False,
        repo_root=root,
        env=env,
    )
    status = "pass" if bridge_report["status"] == "pass" else "fail"
    return {
        "schema_version": 1,
        "status": status,
        "out_dir": str(out_dir),
        "semsql_bin": str(semsql_bin),
        "bin_dir": str(bin_dir),
        "shim_path": str(shim_path),
        "extractor_script": str(extractor_script),
        "workspace_fallback_disabled": True,
        "summary": bridge_report["summary"],
        "bridge_report": bridge_report,
    }


def render_package_bridge_probe_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# Package Bridge Probe",
        "",
        f"- status: `{report['status'].upper()}`",
        f"- workspace fallback disabled: `{report['workspace_fallback_disabled']}`",
        f"- installed bin dir: `{report['bin_dir']}`",
        f"- extractor shim: `{report['shim_path']}`",
        f"- frameworks: `{summary['passed']}/{summary['frameworks']}`",
        f"- expected vocab matched: `{summary['matched_vocab']}/{summary['expected_vocab']}`",
        f"- query checks: `{summary['query_ok']}/{summary['query_checks']}`",
        "",
        "## Framework Bridge Result",
        "",
        render_framework_bridge_probe_markdown(report["bridge_report"]).rstrip(),
        "",
        "## Limits",
        "",
        "This proves native PATH lookup with workspace fallback disabled.",
        "It does not replace a published-package `pnpm dlx` smoke against a tagged release.",
    ]
    return "\n".join(lines) + "\n"


def _write_extractor_shim(bin_dir: Path, extractor_script: Path) -> Path:
    if os.name == "nt":
        shim = bin_dir / "semsql-extract.cmd"
        body = f'@echo off\r\nnode "{extractor_script}" %*\r\n'
        shim.write_text(body, encoding="utf-8")
        return shim
    shim = bin_dir / "semsql-extract"
    body = (
        "#!/usr/bin/env sh\n"
        f"exec node {json.dumps(str(extractor_script))} \"$@\"\n"
    )
    shim.write_text(body, encoding="utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return shim
