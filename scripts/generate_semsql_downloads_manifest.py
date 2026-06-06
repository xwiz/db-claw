#!/usr/bin/env python3
"""Generate semsql-downloads.json for GitHub Release binary assets."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import quote


def parse_asset(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError(
            "asset must use TARGET=PATH, for example linux-x64=target/release/semsql"
        )
    target, raw_path = value.split("=", 1)
    target = target.strip()
    if not target:
        raise argparse.ArgumentTypeError("asset target must not be empty")
    path = Path(raw_path)
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"asset path does not exist: {path}")
    return target, path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def asset_url(base_url: str, path: Path) -> str:
    return f"{base_url.rstrip('/')}/{quote(path.name)}"


def build_manifest(
    *,
    version: str,
    base_url: str,
    assets: list[tuple[str, Path]],
) -> dict[str, object]:
    seen: set[str] = set()
    payload: dict[str, object] = {"version": version, "assets": {}}
    asset_payload = payload["assets"]
    assert isinstance(asset_payload, dict)
    for target, path in assets:
        if target in seen:
            raise ValueError(f"duplicate target: {target}")
        seen.add(target)
        resolved = path.resolve()
        asset_payload[target] = {
            "url": asset_url(base_url, resolved),
            "sha256": sha256_file(resolved),
            "size": resolved.stat().st_size,
        }
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate semsql-downloads.json for @semsql/cli.",
    )
    parser.add_argument("--version", required=True, help="Release version, e.g. 0.1.0-alpha.1.")
    parser.add_argument(
        "--base-url",
        required=True,
        help="Release asset base URL, e.g. https://github.com/org/repo/releases/download/vX.",
    )
    parser.add_argument(
        "--asset",
        action="append",
        type=parse_asset,
        required=True,
        help="Target/path pair. Repeat, e.g. --asset linux-x64=target/release/semsql.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("semsql-downloads.json"),
        help="Output manifest path.",
    )
    args = parser.parse_args()
    manifest = build_manifest(
        version=args.version,
        base_url=args.base_url,
        assets=args.asset,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
