"""Tests for active subset selection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from semsql_train.active_subset import (
    active_subset,
    deterministic_stride_subset,
)


def _has_st() -> bool:
    try:
        import sentence_transformers  # noqa: F401
        import sklearn  # noqa: F401
        import numpy  # noqa: F401
    except ImportError:
        return False
    return True


def _write_pool(tmp_path: Path, n: int) -> Path:
    """Write a synthetic pool of ``n`` rows where each row's NL has a
    distinct prefix — clustering should pick one row per prefix bucket."""
    path = tmp_path / "pool.jsonl"
    rows = []
    for i in range(n):
        rows.append({
            "stage": 2,
            "nl": f"prefix-{i % 10} variant {i}",
            "natsql_skeleton": "SELECT * FROM @entity1",
            "ranked_schema": [{"kind": "entity", "target": "users", "score": 1.0}],
            "slot_map": {"@entity1": "users"},
        })
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, sort_keys=True))
            fh.write("\n")
    return path


class TestStrideSubset:
    """Pure-Python fallback — works without the ML stack."""

    def test_target_larger_than_pool_returns_all(self) -> None:
        rows = [{"i": i} for i in range(5)]
        out = deterministic_stride_subset(rows, target_k=10)
        assert len(out) == 5

    def test_target_zero_returns_empty(self) -> None:
        assert deterministic_stride_subset([{"x": 1}], target_k=0) == []

    def test_uniform_stride(self) -> None:
        rows = [{"i": i} for i in range(100)]
        out = deterministic_stride_subset(rows, target_k=10)
        assert len(out) == 10
        # Stride = 10 → indices 0, 10, 20, …, 90.
        assert [r["i"] for r in out] == [0, 10, 20, 30, 40, 50, 60, 70, 80, 90]

    def test_deterministic(self) -> None:
        rows = [{"i": i} for i in range(50)]
        a = deterministic_stride_subset(rows, target_k=7)
        b = deterministic_stride_subset(rows, target_k=7)
        assert a == b


class TestActiveSubset:
    """Full ML-backed selection. Skipped when sentence-transformers is
    absent so CI on a fresh checkout stays green."""

    @pytest.mark.skipif(
        not _has_st(),
        reason="sentence-transformers not installed — `pip install sentence-transformers`",
    )
    def test_target_at_or_above_pool_passes_through(self, tmp_path: Path) -> None:
        pool = _write_pool(tmp_path, n=20)
        out = tmp_path / "out.jsonl"
        stats = active_subset(pool, out, target_k=20)
        # No clustering — every row carried through.
        assert stats.selected == 20
        lines = out.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 20

    @pytest.mark.skipif(
        not _has_st(),
        reason="sentence-transformers not installed",
    )
    def test_clustering_picks_target_distinct_rows(self, tmp_path: Path) -> None:
        pool = _write_pool(tmp_path, n=200)
        out = tmp_path / "out.jsonl"
        stats = active_subset(pool, out, target_k=20)
        # Every selected row is distinct (cluster representatives).
        rows = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines()]
        nls = [r["nl"] for r in rows]
        assert len(rows) == 20
        assert len(set(nls)) == 20
        assert stats.selected == 20

    def test_empty_pool_raises(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.jsonl"
        empty.write_text("", encoding="utf-8")
        with pytest.raises(RuntimeError):
            active_subset(empty, tmp_path / "out.jsonl", target_k=5)
