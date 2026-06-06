"""Dataset fetch/materialization helpers for eval corpora."""

from __future__ import annotations

import json
import shutil
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

BIRD_TRAIN_URL = "https://bird-bench.oss-cn-beijing.aliyuncs.com/train.zip"
DEFAULT_BIRD_TRAIN_MIN_FREE_GB = 40.0


@dataclass(frozen=True)
class BirdTrainMaterializeResult:
    """Normalized paths written from the official BIRD train archive."""

    train_json: Path
    train_databases: Path
    extracted_files: int


def gibibytes(value: float) -> int:
    return int(value * 1024**3)


def ensure_min_free_space(path: Path, required_bytes: int) -> None:
    """Raise if the filesystem containing ``path`` has too little free space."""
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    free = shutil.disk_usage(probe).free
    if free < required_bytes:
        required_gb = required_bytes / 1024**3
        free_gb = free / 1024**3
        raise RuntimeError(
            f"not enough free disk space under {probe}: "
            f"{free_gb:.1f} GiB free, {required_gb:.1f} GiB required"
        )


def download_file(url: str, out_path: Path, *, force: bool = False) -> Path:
    """Download ``url`` to ``out_path`` unless it already exists."""
    if out_path.exists() and not force:
        return out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()
    with urllib.request.urlopen(url) as response, tmp.open("wb") as fh:  # noqa: S310
        shutil.copyfileobj(response, fh)
    tmp.replace(out_path)
    return out_path


def safe_extract_zip(zip_path: Path, dest_dir: Path) -> int:
    """Extract ``zip_path`` while rejecting absolute/traversal members."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_root = dest_dir.resolve()
    extracted = 0
    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
        for info in infos:
            _safe_zip_target(dest_root, info.filename)
        for info in infos:
            zf.extract(info, dest_root)
            if not info.is_dir():
                extracted += 1
    return extracted


def materialize_official_bird_train_archive(
    archive_path: Path,
    bird_dir: Path,
) -> BirdTrainMaterializeResult:
    """Normalize official BIRD ``train.zip`` into eval-harness layout.

    Output layout:

    ``<bird_dir>/train.json``
    ``<bird_dir>/train_databases/<db_id>/<db_id>.sqlite``
    """
    raw_dir = bird_dir / "raw" / "train_zip"
    if raw_dir.exists():
        shutil.rmtree(raw_dir)
    extracted = safe_extract_zip(archive_path, raw_dir)
    train_json = _find_required_file(raw_dir, "train.json")
    db_payload = _find_train_databases_payload(raw_dir)

    normalized_train = bird_dir / "train.json"
    normalized_train.parent.mkdir(parents=True, exist_ok=True)
    normalized_train.write_text(
        _normalize_bird_json(train_json.read_text(encoding="utf-8")),
        encoding="utf-8",
    )

    normalized_db_root = bird_dir / "train_databases"
    if db_payload.is_dir():
        shutil.copytree(db_payload, normalized_db_root, dirs_exist_ok=True)
    else:
        extracted += _extract_bird_database_zip(db_payload, bird_dir, normalized_db_root)
    shutil.rmtree(raw_dir, ignore_errors=True)
    return BirdTrainMaterializeResult(
        train_json=normalized_train,
        train_databases=normalized_db_root,
        extracted_files=extracted,
    )


def bird_train_is_materialized(bird_dir: Path) -> bool:
    return (bird_dir / "train.json").exists() and (bird_dir / "train_databases").is_dir()


def _safe_zip_target(dest_root: Path, member_name: str) -> Path:
    normalized = member_name.replace("\\", "/")
    posix = PurePosixPath(normalized)
    windows = PureWindowsPath(member_name)
    if not posix.parts:
        raise RuntimeError(f"refusing empty zip member: {member_name!r}")
    if posix.is_absolute() or windows.is_absolute():
        raise RuntimeError(f"refusing absolute zip member: {member_name!r}")
    if any(part in ("", ".", "..") for part in posix.parts):
        raise RuntimeError(f"refusing unsafe zip member: {member_name!r}")
    target = (dest_root / Path(*posix.parts)).resolve()
    try:
        target.relative_to(dest_root)
    except ValueError as exc:
        raise RuntimeError(f"zip member escapes destination: {member_name!r}") from exc
    return target


def _find_required_file(root: Path, name: str) -> Path:
    matches = [p for p in root.rglob(name) if p.is_file()]
    if not matches:
        raise RuntimeError(f"{name} not found under extracted BIRD archive {root}")
    return matches[0]


def _find_train_databases_payload(root: Path) -> Path:
    for p in root.rglob("*"):
        if p.is_file() and p.name in {"train_databases.zip", "train_databases"}:
            return p
    dirs = [p for p in root.rglob("train_databases") if p.is_dir()]
    if dirs:
        return dirs[0]
    raise RuntimeError(f"train_databases payload not found under {root}")


def _extract_bird_database_zip(db_zip: Path, bird_dir: Path, normalized_db_root: Path) -> int:
    with zipfile.ZipFile(db_zip) as zf:
        roots = {
            PurePosixPath(info.filename.replace("\\", "/")).parts[0]
            for info in zf.infolist()
            if PurePosixPath(info.filename.replace("\\", "/")).parts
        }
    if "train_databases" in roots:
        return safe_extract_zip(db_zip, bird_dir)
    return safe_extract_zip(db_zip, normalized_db_root)


def _normalize_bird_json(text: str) -> str:
    data = json.loads(text)
    return json.dumps(data, indent=2) + "\n"
