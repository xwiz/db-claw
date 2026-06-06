#!/usr/bin/env python3
"""Fail fast when generated artifacts are about to be committed.

GitHub blocks normal Git pushes containing files over 100 MiB and warns on
large files before that. This repo keeps generated data, models, raw reports,
and local binaries out of Git; release assets belong in GitHub Releases.
"""

from __future__ import annotations

import argparse
import fnmatch
import shutil
import subprocess
from pathlib import Path

BLOCKED_DIR_PREFIXES = (
    ".mypy_cache/",
    ".playwright-mcp/",
    ".pytest_cache/",
    ".ruff_cache/",
    ".venv/",
    "artifacts/",
    "data/",
    "logs/",
    "node_modules/",
    "reports/",
    "target/",
    "venv/",
)

BLOCKED_PATTERNS = (
    "docs/results/*.json",
    "docs/results/**/*.json",
    "models/fine-tunes/**",
)

ALLOWED_PATTERNS = (
    "crates/*/tests/fixtures/*.jsonl",
    "python/*/tests/fixtures/*.jsonl",
)

BLOCKED_SUFFIXES = (
    ".a",
    ".db",
    ".dll",
    ".duckdb",
    ".dylib",
    ".exe",
    ".gz",
    ".jsonl",
    ".lib",
    ".log",
    ".onnx",
    ".parquet",
    ".pdb",
    ".semsql",
    ".so",
    ".sqlite",
    ".sqlite3",
    ".tar",
    ".tgz",
    ".zip",
)


def run_git(root: Path, args: list[str]) -> list[str]:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git executable not found")
    proc = subprocess.run(
        [git, *args],
        cwd=root,
        check=True,
        capture_output=True,
    )
    data = proc.stdout.decode("utf-8", errors="replace")
    return [item for item in data.split("\0") if item]


def repo_root() -> Path:
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("git executable not found")
    proc = subprocess.run(
        [git, "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
    )
    return Path(proc.stdout.decode("utf-8").strip()).resolve()


def staged_paths(root: Path) -> list[str]:
    return run_git(root, ["diff", "--cached", "--name-only", "-z"])


def visible_paths(root: Path) -> list[str]:
    return run_git(
        root,
        ["ls-files", "--cached", "--others", "--exclude-standard", "-z"],
    )


def normalized(path: str) -> str:
    return path.replace("\\", "/")


def blocked_reason(path: str) -> str | None:
    lower = normalized(path).lower()
    for pattern in ALLOWED_PATTERNS:
        if fnmatch.fnmatch(lower, pattern.lower()):
            return None
    for prefix in BLOCKED_DIR_PREFIXES:
        if lower.startswith(prefix):
            return f"generated/local directory `{prefix}`"
    for pattern in BLOCKED_PATTERNS:
        if fnmatch.fnmatch(lower, pattern.lower()):
            return f"generated artifact pattern `{pattern}`"
    for suffix in BLOCKED_SUFFIXES:
        if lower.endswith(suffix):
            return f"generated/binary suffix `{suffix}`"
    return None


def file_size_mib(root: Path, path: str) -> float:
    full = root / path
    if not full.exists() or not full.is_file():
        return 0.0
    return full.stat().st_size / (1024 * 1024)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--all",
        action="store_true",
        help="scan all tracked and visible untracked files instead of staged files",
    )
    parser.add_argument(
        "--max-mib",
        type=float,
        default=50.0,
        help="fail when a visible file is larger than this size",
    )
    args = parser.parse_args()

    root = repo_root()
    paths = visible_paths(root) if args.all else staged_paths(root)
    bad: list[str] = []

    for path in paths:
        reason = blocked_reason(path)
        size_mib = file_size_mib(root, path)
        if reason:
            bad.append(f"{path} ({reason})")
        elif size_mib > args.max_mib:
            bad.append(f"{path} ({size_mib:.2f} MiB > {args.max_mib:.2f} MiB)")

    if not bad:
        scanned = "visible files" if args.all else "staged files"
        print(f"artifact check passed for {len(paths)} {scanned}")
        return 0

    print("Do not commit generated artifacts or GitHub-hostile files:\n")
    for item in bad:
        print(f"  - {item}")
    print("\nPut generated outputs under target/, reports/, artifacts/, or a release asset.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
