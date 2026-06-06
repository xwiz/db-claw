from __future__ import annotations

import json
import shutil
import zipfile
from collections import namedtuple
from pathlib import Path

import pytest
from click.testing import CliRunner
from semsql_eval.__main__ import cli
from semsql_eval.datasets import (
    ensure_min_free_space,
    materialize_official_bird_train_archive,
    safe_extract_zip,
)


def test_safe_extract_zip_rejects_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../evil.txt", "nope")

    with pytest.raises(RuntimeError, match="unsafe zip member"):
        safe_extract_zip(archive, tmp_path / "out")

    assert not (tmp_path / "evil.txt").exists()


def test_safe_extract_zip_rejects_windows_absolute_path(tmp_path: Path) -> None:
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("C:/temp/evil.txt", "nope")

    with pytest.raises(RuntimeError, match="absolute zip member"):
        safe_extract_zip(archive, tmp_path / "out")


def test_materialize_official_bird_train_archive_normalizes_layout(
    tmp_path: Path,
) -> None:
    inner = tmp_path / "train_databases.zip"
    with zipfile.ZipFile(inner, "w") as zf:
        zf.writestr("demo/demo.sqlite", "sqlite bytes")

    outer = tmp_path / "train.zip"
    train_rows = [{"db_id": "demo", "question": "q", "SQL": "SELECT 1"}]
    with zipfile.ZipFile(outer, "w") as zf:
        zf.writestr("train/train.json", json.dumps(train_rows))
        zf.write(inner, "train/train_databases.zip")

    result = materialize_official_bird_train_archive(outer, tmp_path / "bird")

    assert result.train_json == tmp_path / "bird" / "train.json"
    assert json.loads(result.train_json.read_text(encoding="utf-8")) == train_rows
    assert (tmp_path / "bird" / "train_databases" / "demo" / "demo.sqlite").exists()


def test_ensure_min_free_space_raises_with_clear_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    usage = namedtuple("usage", ["total", "used", "free"])

    monkeypatch.setattr(shutil, "disk_usage", lambda _path: usage(100, 90, 10))

    with pytest.raises(RuntimeError, match="not enough free disk space"):
        ensure_min_free_space(tmp_path / "missing" / "child", 11)


def test_fetch_bird_train_requires_databases() -> None:
    result = CliRunner().invoke(
        cli,
        ["fetch-datasets", "--suite", "bird", "--bird-split", "train"],
    )

    assert result.exit_code == 2
    assert "requires --with-databases" in result.output
