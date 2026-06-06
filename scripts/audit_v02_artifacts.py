#!/usr/bin/env python3
"""Audit v0.2 benchmark artifacts before deleting local files.

The important rule is that manifests are dependency roots. Reports can point to
regenerable caches, but a manifest that references missing ONNX/tokenizer files
means model-backed evals cannot run.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class PathRef:
    source: Path
    path: Path
    kind: str
    required: bool


@dataclass
class Audit:
    refs: list[PathRef] = field(default_factory=list)

    def add(self, source: Path, path: Path, kind: str, required: bool) -> None:
        self.refs.append(PathRef(source=source, path=path, kind=kind, required=required))


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def load_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def resolve_repo_path(root: Path, value: str) -> Path | None:
    normalized = value.replace("\\", "/")
    if normalized.startswith("target/") or normalized.startswith("docs/results/"):
        return (root / normalized).resolve()
    return None


REPORT_PATH_KEYS = {
    "cascade_manifest",
    "graph_cache_dir",
    "graph_cache_path",
    "graph_path",
    "manifest_path",
    "query_frame_dir",
    "query_frame_path",
    "trace_path",
}


def walk_report_paths(value: Any, key: str | None = None) -> list[str]:
    """Return artifact paths from report data, excluding command metadata.

    Eval reports contain command argv fields such as ``target/debug/semsql.exe``
    and ``--report-json target/v02/...``. Those are provenance, not retained
    artifacts. Keep this intentionally allowlisted so the audit cannot promote a
    stale command-line string into a cleanup blocker.
    """

    out: list[str] = []
    if isinstance(value, str):
        if key in REPORT_PATH_KEYS:
            out.append(value)
    elif isinstance(value, list):
        for item in value:
            out.extend(walk_report_paths(item, key=None))
    elif isinstance(value, dict):
        for child_key, item in value.items():
            out.extend(walk_report_paths(item, key=str(child_key)))
    return out


def manifest_refs(root: Path, manifest: Path, audit: Audit) -> None:
    data = load_json(manifest)
    if not isinstance(data, dict):
        return
    parent = manifest.parent
    audit.add(manifest, manifest.resolve(), "manifest_file", True)
    for stage in ("linker", "skeleton", "slot_filler"):
        block = data.get(stage)
        if not isinstance(block, dict):
            continue
        for key in ("path", "tokenizer"):
            raw = block.get(key)
            if not isinstance(raw, str) or not raw:
                continue
            path = Path(raw)
            resolved = path if path.is_absolute() else parent / path
            audit.add(manifest, resolved.resolve(), f"manifest:{stage}:{key}", True)


def report_refs(root: Path, report: Path, audit: Audit) -> None:
    data = load_json(report)
    if data is None:
        return
    for raw in walk_report_paths(data):
        resolved = resolve_repo_path(root, raw)
        if resolved is not None:
            audit.add(report, resolved, "report_reference", False)


def path_status(path: Path) -> str:
    if path.exists():
        return "present"
    return "missing"


def is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def dir_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    if path.exists():
        for current, _, files in os.walk(path):
            for name in files:
                try:
                    total += (Path(current) / name).stat().st_size
                except OSError:
                    pass
    return total


def mb(size: int) -> str:
    return f"{size / (1024 * 1024):.2f} MB"


def build_audit(root: Path) -> Audit:
    audit = Audit()
    for manifest in sorted((root / "target" / "v02").glob("**/manifest.json")):
        manifest_refs(root, manifest, audit)
    for report in sorted((root / "docs" / "results").glob("*.json")):
        report_refs(root, report, audit)
    return audit


def print_missing(audit: Audit, root: Path) -> int:
    missing_required = [
        ref for ref in audit.refs if ref.required and not ref.path.exists()
    ]
    missing_optional = [
        ref for ref in audit.refs if not ref.required and not ref.path.exists()
    ]
    if missing_required:
        print("Missing required manifest dependencies:")
        for ref in missing_required:
            print(
                f"  - {ref.path.relative_to(root)}"
                f"  ({ref.kind}, from {ref.source.relative_to(root)})"
            )
    else:
        print("Missing required manifest dependencies: none")
    if missing_optional:
        print("\nMissing optional report references:")
        for ref in missing_optional[:50]:
            print(
                f"  - {ref.path.relative_to(root)}"
                f"  ({ref.kind}, from {ref.source.relative_to(root)})"
            )
        if len(missing_optional) > 50:
            print(f"  ... {len(missing_optional) - 50} more")
    return 1 if missing_required else 0


def print_target_inventory(audit: Audit, root: Path) -> None:
    target_v02 = root / "target" / "v02"
    required = {ref.path for ref in audit.refs if ref.required}
    optional = {ref.path for ref in audit.refs if not ref.required}
    print("\ntarget/v02 inventory:")
    for child in sorted(target_v02.iterdir(), key=lambda p: p.name):
        resolved = child.resolve()
        hard = any(resolved == p or is_within(p, resolved) for p in required)
        soft = any(resolved == p or is_within(p, resolved) for p in optional)
        if hard:
            decision = "KEEP: manifest dependency"
        elif soft:
            decision = "KEEP: referenced by retained report"
        else:
            decision = "candidate: unreferenced local artifact"
        print(f"  - {child.name:45} {mb(dir_size(child)):>10}  {decision}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=repo_root())
    args = parser.parse_args()
    root = args.root.resolve()
    audit = build_audit(root)
    code = print_missing(audit, root)
    print_target_inventory(audit, root)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
