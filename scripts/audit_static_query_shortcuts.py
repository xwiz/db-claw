#!/usr/bin/env python3
"""Fail when production runtime code grows app/query-specific shortcuts."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import NamedTuple

PRODUCTION_RUST_ROOTS = (
    Path("crates/semsql-runtime/src"),
    Path("crates/semsql-cli/src"),
)

BANNED_LITERALS = (
    "Marvel Comics",
    "DC Comics",
    "cloudspace",
    "elbiblio",
    "final_approval",
    "fraud_reports",
    "hostshell",
    "mailer_web",
    "publisher_name",
    "scored 20",
    "show transactions",
    "superhero",
    "least intelligent",
    "highest intelligence",
    "minimum intelligence",
    "Durability",
    "Combat",
)

BANNED_REGEXES = (
    re.compile(r'tokens\.contains\("students"\)\s*\{\s*tokens\.insert\("enrollment"', re.S),
    re.compile(r'lower_context\.contains\("publisher"\)', re.S),
    re.compile(r'lower_nl\.contains\("charter"\).*?tokens\.contains\("charter"\)', re.S),
    re.compile(r'tail_lower\.contains\("cdscode"\)', re.S),
    re.compile(r'tail_lower\s*==\s*"school"\s*\|\|\s*tail_lower\s*==\s*"sname"', re.S),
    re.compile(r'"school",\s*"schools",\s*"student",\s*"students"', re.S),
    re.compile(r'"code"\s*=>\s*\[[^\]]*"cds"', re.S),
    re.compile(r'"code"\s*=>\s*\[[^\]]*"charter"', re.S),
    re.compile(r'"enum_keyword"\s*=>\s*\[[^\]]*"charter"', re.S),
    re.compile(r'"phrase"\s*\|\s*"quoted_string"\s*=>\s*\[[^\]]*"school"', re.S),
)


class Finding(NamedTuple):
    path: Path
    line: int
    pattern: str
    text: str


def audit_static_query_shortcuts(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for source_root in PRODUCTION_RUST_ROOTS:
        full_root = root / source_root
        if not full_root.exists():
            continue
        for path in sorted(full_root.rglob("*.rs")):
            relative = path.relative_to(root)
            text = _production_rust_text(path)
            findings.extend(_literal_findings(relative, text))
            findings.extend(_regex_findings(relative, text))
    return findings


def _production_rust_text(path: Path) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    production: list[str] = []
    for line in lines:
        if line.strip() == "#[cfg(test)]":
            break
        production.append(line)
    return "\n".join(production)


def _literal_findings(path: Path, text: str) -> list[Finding]:
    findings: list[Finding] = []
    lowered = text.lower()
    for literal in BANNED_LITERALS:
        start = 0
        needle = literal.lower()
        while True:
            index = lowered.find(needle, start)
            if index == -1:
                break
            line = text.count("\n", 0, index) + 1
            line_text = text.splitlines()[line - 1].strip()
            findings.append(Finding(path, line, literal, line_text))
            start = index + len(needle)
    return findings


def _regex_findings(path: Path, text: str) -> list[Finding]:
    findings: list[Finding] = []
    for pattern in BANNED_REGEXES:
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            line_text = text.splitlines()[line - 1].strip()
            findings.append(Finding(path, line, pattern.pattern, line_text))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args()

    findings = audit_static_query_shortcuts(args.root.resolve())
    if findings:
        print("static shortcut audit failed:\n")
        for finding in findings:
            print(
                f"  - {finding.path}:{finding.line}: {finding.pattern}: {finding.text}"
            )
        print(
            "\nMove app/database meaning into graph extraction, sample values, "
            "vocabulary, metric catalogs, or typed atlas metadata."
        )
        return 1
    print("static shortcut audit passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
