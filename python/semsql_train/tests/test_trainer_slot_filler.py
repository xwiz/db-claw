from __future__ import annotations

from pathlib import Path

import pytest
from semsql_train.generators import GeneratorConfig, generate_slot_pairs
from semsql_train.trainers.slot_filler import (
    SlotFillerTrainConfig,
    build_dataset,
    preflight,
    train_slot_filler,
    write_jsonl,
)

from tests.fixtures.make_graph import build as build_graph


@pytest.fixture
def real_train_data(tmp_path: Path) -> tuple[Path, Path]:
    graph = build_graph(tmp_path / "g.semsql")
    cfg = GeneratorConfig(paraphrase_variants=2, seed=1)
    records = list(generate_slot_pairs(str(graph), cfg))
    train = tmp_path / "train.jsonl"
    eval_ = tmp_path / "eval.jsonl"
    split = max(int(len(records) * 0.8), 1)
    write_jsonl(records[:split], train)
    write_jsonl(records[split:] or records[:1], eval_)
    return train, eval_


def _good_record() -> dict:
    return {
        "nl": "show students",
        "skeleton": "SELECT * FROM @entity1",
        "slot_name": "@entity1",
        "candidates": ["users", "tenants"],
        "correct_index": 0,
    }


class TestBuildDataset:
    def test_streams_records(self, tmp_path: Path) -> None:
        path = tmp_path / "x.jsonl"
        write_jsonl([_good_record()], path)
        records = list(build_dataset(path))
        assert records[0]["slot_name"] == "@entity1"

    def test_missing_required_key_raises(self, tmp_path: Path) -> None:
        rec = _good_record()
        del rec["candidates"]
        path = tmp_path / "bad.jsonl"
        write_jsonl([rec], path)
        with pytest.raises(ValueError, match="candidates"):
            list(build_dataset(path))

    def test_empty_candidates_raises(self, tmp_path: Path) -> None:
        rec = _good_record()
        rec["candidates"] = []
        path = tmp_path / "bad.jsonl"
        write_jsonl([rec], path)
        with pytest.raises(ValueError, match="non-empty"):
            list(build_dataset(path))

    def test_out_of_range_correct_index_raises(self, tmp_path: Path) -> None:
        rec = _good_record()
        rec["correct_index"] = 99
        path = tmp_path / "bad.jsonl"
        write_jsonl([rec], path)
        with pytest.raises(ValueError, match="out of range"):
            list(build_dataset(path))

    def test_oversized_candidate_list_raises(self, tmp_path: Path) -> None:
        rec = _good_record()
        rec["candidates"] = [f"c{i}" for i in range(64)]
        rec["correct_index"] = 0
        path = tmp_path / "bad.jsonl"
        write_jsonl([rec], path)
        with pytest.raises(ValueError, match="sanity cap"):
            list(build_dataset(path))


class TestPreflight:
    def test_passes_on_real_data(
        self, real_train_data: tuple[Path, Path], tmp_path: Path
    ) -> None:
        train, eval_ = real_train_data
        cfg = SlotFillerTrainConfig(
            train_jsonl=train, eval_jsonl=eval_, output_dir=tmp_path / "out"
        )
        report = preflight(cfg)
        assert report.ok, f"unexpected issues: {report.issues}"
        assert report.train_count > 0
        assert report.avg_candidates >= 1.5

    def test_flags_degenerate_single_candidate(self, tmp_path: Path) -> None:
        rec = _good_record()
        rec["candidates"] = ["users"]
        rec["correct_index"] = 0
        train = tmp_path / "train.jsonl"
        eval_ = tmp_path / "eval.jsonl"
        write_jsonl([rec, rec, rec], train)
        write_jsonl([rec], eval_)
        cfg = SlotFillerTrainConfig(
            train_jsonl=train, eval_jsonl=eval_, output_dir=tmp_path / "out"
        )
        report = preflight(cfg)
        assert any("candidate-list size" in i for i in report.issues)


class TestTrainSlotFiller:
    def test_blocks_when_preflight_fails(self, tmp_path: Path) -> None:
        cfg = SlotFillerTrainConfig(
            train_jsonl=tmp_path / "missing.jsonl",
            eval_jsonl=tmp_path / "missing.jsonl",
            output_dir=tmp_path / "out",
        )
        with pytest.raises(RuntimeError, match="preflight failed"):
            train_slot_filler(cfg)
