#!/usr/bin/env python3
"""Rehearse npm/Rust release packaging without publishing.

The rehearsal temporarily applies the requested non-dev release version to the
release version files, runs the same version/package/tarball checks used by the
release workflow, then restores the original files before exiting.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from check_npm_release_packages import check_release_packages  # noqa: E402
from check_release_versions import check_release_versions  # noqa: E402
from prepare_release_versions import (  # noqa: E402
    NPM_RELEASE_PACKAGE_DIRS,
    prepare_release_versions,
)


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


def release_version_paths(root: Path) -> list[Path]:
    return [
        root / "Cargo.toml",
        root / "package.json",
        *[
            root / "packages" / package_dir / "package.json"
            for package_dir in NPM_RELEASE_PACKAGE_DIRS
        ],
    ]


def snapshot_files(paths: list[Path]) -> dict[Path, bytes]:
    missing = [path for path in paths if not path.is_file()]
    if missing:
        raise RuntimeError(
            "release version file missing: "
            + ", ".join(str(path) for path in missing)
        )
    return {path: path.read_bytes() for path in paths}


def restore_files(snapshot: dict[Path, bytes]) -> None:
    for path, content in snapshot.items():
        path.write_bytes(content)


def rehearse_release_packages(
    *,
    root: Path,
    version: str,
    package_manager: str = "pnpm",
    run_checks: bool = True,
    pack_destination: Path | None = None,
) -> dict[str, Any]:
    paths = release_version_paths(root)
    snapshot = snapshot_files(paths)
    destination = pack_destination or root / "target" / "release_rehearsal" / "npm"
    restored = False
    restore_error: str | None = None
    report: dict[str, Any] = {
        "schema_version": 1,
        "status": "fail",
        "version": version,
        "package_manager": package_manager,
        "run_checks": run_checks,
        "pack_destination": str(destination),
    }
    try:
        prepare_report = prepare_release_versions(
            root=root,
            version=version,
            apply=True,
        )
        version_report = check_release_versions(
            root=root,
            expected_version=version,
            surfaces=("rust", "npm"),
        )
        package_report = check_release_packages(
            root=root,
            expected_version=version,
            package_manager=package_manager,
            run_checks=run_checks,
            pack_destination=destination,
            clean_pack_destination=True,
        )
        report.update(
            {
                "status": (
                    "pass"
                    if prepare_report["status"] == "pass"
                    and version_report["status"] == "pass"
                    and package_report["status"] == "pass"
                    else "fail"
                ),
                "prepare": prepare_report,
                "version_check": version_report,
                "package_check": package_report,
            }
        )
    finally:
        try:
            restore_files(snapshot)
            restored = all(
                path.read_bytes() == content for path, content in snapshot.items()
            )
        except Exception as exc:  # pragma: no cover - defensive restore evidence
            restore_error = str(exc)
        report["restored"] = restored
        report["restore_error"] = restore_error

    if not restored:
        report["status"] = "fail"
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True, help="Release version to rehearse.")
    parser.add_argument("--package-manager", default="pnpm")
    parser.add_argument(
        "--skip-run-checks",
        action="store_true",
        help="Pack without running each package lint/test/typecheck script.",
    )
    parser.add_argument(
        "--pack-destination",
        type=Path,
        help="Where to write rehearsal tarballs. Defaults under target/.",
    )
    parser.add_argument("--out-json", type=Path)
    args = parser.parse_args()

    report = rehearse_release_packages(
        root=repo_root(),
        version=args.version,
        package_manager=args.package_manager,
        run_checks=not args.skip_run_checks,
        pack_destination=args.pack_destination,
    )
    rendered = json.dumps(report, indent=2) + "\n"
    if args.out_json is not None:
        args.out_json.parent.mkdir(parents=True, exist_ok=True)
        args.out_json.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
