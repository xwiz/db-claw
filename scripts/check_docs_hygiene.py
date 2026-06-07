#!/usr/bin/env python3
"""Keep living docs compact while preserving retained evidence reports."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
from pathlib import Path

LIVING_DOC_LIMITS = {
    "README.md": (120, 10.0),
    "docs/README.md": (80, 8.0),
    "docs/ARCHITECTURE.md": (95, 4.0),
    "docs/COMPARISONS.md": (95, 4.5),
    "docs/CONTRIBUTING.md": (125, 9.0),
    "docs/results/README.md": (24, 2.0),
    "docs/results/v02-current-status.md": (5, 0.6),
    "docs/results/v02-evidence-ledger.md": (12, 2.2),
    "docs/results/v02-quality-gate.md": (30, 2.4),
    "docs/results/v02-semantic-atlas-completion-plan.md": (26, 2.2),
    "docs/GO_LIVE.md": (110, 8.0),
}

CURRENT_LOOKING_RETAINED_LIMIT = (80, 4.0)

CURRENT_LOOKING_NAMES = (
    "current",
    "status",
    "plan",
    "gate",
    "go-live",
    "completion",
    "hardening",
)

STRATEGY_LOOKING_NAMES = (
    "approach",
    "expectation",
    "ladder",
    "path",
    "pivot",
    "strategy",
)

HISTORICAL_BENCHMARK_MARKERS = (
    "v02-recovery-manifest-full-bird-dev-v",
    "v02-livepath",
    "v02-residual-failures-v17",
)

RETAINED_REPORT_NAMES = (
    "benchmark",
    "gate-report",
    "recovery-manifest",
    "livepath",
    "smoke",
    "probe",
    "suite",
    "audit",
    "fallback",
    "packet",
    "canary",
    "slice",
)

PROVENANCE_DATE_RE = re.compile(r"\b20\d{2}-\d{2}-\d{2}\b")
PROVENANCE_VERSION_RE = re.compile(
    r"(?i)(package version|version:\s*`?\d|@semsql/cli@|"
    r"semsql\s+\d+\.\d+\.\d+|v\d+\.\d+\.\d+|0\.1\.0-)"
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


def line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def size_kib(path: Path) -> float:
    return path.stat().st_size / 1024


def has_result_provenance(path: Path) -> bool:
    header = "\n".join(path.read_text(encoding="utf-8").splitlines()[:10])
    return bool(PROVENANCE_DATE_RE.search(header) or PROVENANCE_VERSION_RE.search(header))


def changed_result_markdown(root: Path) -> list[Path]:
    git = shutil.which("git")
    if git is None:
        return []
    proc = subprocess.run(
        [git, "status", "--short", "--", "docs/results"],
        check=True,
        capture_output=True,
        text=True,
        cwd=root,
    )
    paths: list[Path] = []
    for line in proc.stdout.splitlines():
        if len(line) < 4:
            continue
        raw = line[3:]
        if " -> " in raw:
            raw = raw.split(" -> ", 1)[1]
        path = root / raw
        if path.suffix == ".md" and path.exists() and path.name != "README.md":
            paths.append(path)
    return sorted(paths)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--top",
        type=int,
        default=0,
        help="also print the largest retained docs/results Markdown reports",
    )
    parser.add_argument(
        "--warn-current-looking",
        action="store_true",
        help=(
            "warn when docs/results files with current-looking names are not "
            "registered as compact living docs"
        ),
    )
    parser.add_argument(
        "--fail-current-looking",
        action="store_true",
        help=(
            "fail when an unregistered current-looking docs/results file is "
            "larger than the retained-report compactness limit"
        ),
    )
    parser.add_argument(
        "--fail-unregistered-current-looking",
        action="store_true",
        help=(
            "fail when a current-looking docs/results file is not registered "
            "as a compact living doc, even when it is still small"
        ),
    )
    parser.add_argument(
        "--fail-large-retained",
        action="store_true",
        help="fail when retained docs/results reports exceed retained limits",
    )
    parser.add_argument(
        "--fail-missing-historical-banner",
        action="store_true",
        help=(
            "fail when known superseded or strategy-looking reports do not "
            "declare their historical/retained status near the top"
        ),
    )
    parser.add_argument(
        "--fail-missing-provenance-for-changed",
        action="store_true",
        help=(
            "fail when changed docs/results Markdown reports do not declare "
            "a date stamp or exact package/version provenance near the top"
        ),
    )
    parser.add_argument(
        "--fail-missing-provenance",
        action="store_true",
        help=(
            "fail when any docs/results Markdown report does not declare a "
            "date stamp or exact package/version provenance near the top"
        ),
    )
    parser.add_argument(
        "--retained-max-lines",
        type=int,
        default=80,
        help="line limit used with --fail-large-retained",
    )
    parser.add_argument(
        "--retained-max-kib",
        type=float,
        default=3.6,
        help="size limit used with --fail-large-retained",
    )
    args = parser.parse_args()

    root = repo_root()
    failures: list[str] = []

    for relative, (max_lines, max_kib) in LIVING_DOC_LIMITS.items():
        path = root / relative
        if not path.exists():
            failures.append(f"{relative} is missing")
            continue
        lines = line_count(path)
        kib = size_kib(path)
        if lines > max_lines:
            failures.append(f"{relative} has {lines} lines > {max_lines}")
        if kib > max_kib:
            failures.append(f"{relative} is {kib:.1f} KiB > {max_kib:.1f} KiB")

    registered = {Path(relative).as_posix() for relative in LIVING_DOC_LIMITS}

    if (
        args.warn_current_looking
        or args.fail_current_looking
        or args.fail_unregistered_current_looking
    ):
        max_lines, max_kib = CURRENT_LOOKING_RETAINED_LIMIT
        for path in sorted((root / "docs/results").glob("*.md")):
            relative = path.relative_to(root).as_posix()
            if relative in registered:
                continue
            lowered = path.name.lower()
            if any(marker in lowered for marker in RETAINED_REPORT_NAMES):
                continue
            if any(marker in lowered for marker in CURRENT_LOOKING_NAMES):
                message = (
                    "current-looking retained doc is not registered as a "
                    f"living doc: {relative}"
                )
                too_large = line_count(path) > max_lines or size_kib(path) > max_kib
                if args.fail_unregistered_current_looking:
                    failures.append(message)
                elif args.fail_current_looking and too_large:
                    failures.append(
                        f"{message} and exceeds {max_lines} lines/{max_kib:.1f} KiB"
                    )
                else:
                    print(f"warning: {message}")

    if args.fail_large_retained:
        for path in sorted((root / "docs/results").glob("*.md")):
            relative = path.relative_to(root).as_posix()
            if relative in registered:
                continue
            lines = line_count(path)
            kib = size_kib(path)
            if lines > args.retained_max_lines or kib > args.retained_max_kib:
                failures.append(
                    "retained report exceeds compactness limit: "
                    f"{relative} has {lines} lines/{kib:.1f} KiB "
                    f"> {args.retained_max_lines} lines/"
                    f"{args.retained_max_kib:.1f} KiB"
                )

    if args.fail_missing_historical_banner:
        for path in sorted((root / "docs/results").glob("*.md")):
            lowered = path.name.lower()
            needs_banner = any(
                marker in lowered for marker in HISTORICAL_BENCHMARK_MARKERS
            ) or any(marker in lowered for marker in STRATEGY_LOOKING_NAMES)
            if not needs_banner:
                continue
            header = "\n".join(path.read_text(encoding="utf-8").splitlines()[:6])
            lowered_header = header.lower()
            if "historical" not in lowered_header and "retained" not in lowered_header:
                failures.append(
                    "superseded/strategy-looking report is missing a retained "
                    f"banner near the top: {path.relative_to(root).as_posix()}"
                )

    provenance_paths: list[Path] = []
    if args.fail_missing_provenance:
        provenance_paths = [
            path
            for path in sorted((root / "docs/results").glob("*.md"))
            if path.name != "README.md"
        ]
    elif args.fail_missing_provenance_for_changed:
        provenance_paths = changed_result_markdown(root)
    for path in provenance_paths:
        if not has_result_provenance(path):
            failures.append(
                "result report is missing date/version provenance near the top: "
                f"{path.relative_to(root).as_posix()}"
            )

    if args.top > 0:
        reports = sorted(
            (path for path in (root / "docs/results").glob("*.md")),
            key=lambda item: item.stat().st_size,
            reverse=True,
        )
        print("largest retained docs/results reports:")
        for path in reports[: args.top]:
            relative = path.relative_to(root).as_posix()
            print(f"  {size_kib(path):5.1f} KiB  {relative}")

    if failures:
        print("docs hygiene check failed:\n")
        for failure in failures:
            print(f"  - {failure}")
        print(
            "\nMove long run detail into a retained report and keep living docs "
            "as dashboards, ledgers, or checklists."
        )
        return 1

    print(f"docs hygiene passed for {len(LIVING_DOC_LIMITS)} living docs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
