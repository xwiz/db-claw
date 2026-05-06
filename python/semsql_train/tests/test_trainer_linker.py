from __future__ import annotations

from pathlib import Path

import pytest

from semsql_train.generators import GeneratorConfig, generate_linker_pairs
from semsql_train.trainers.linker import (
    LinkerTrainConfig,
    build_dataset,
    preflight,
    train_linker,
    write_jsonl,
)

from tests.fixtures.make_graph import build as build_graph


@pytest.fixture
def real_train_data(tmp_path: Path) -> tuple[Path, Path]:
    graph = build_graph(tmp_path / "g.semsql")
    cfg = GeneratorConfig(paraphrase_variants=2, seed=1)
    records = list(generate_linker_pairs(str(graph), cfg))
    train = tmp_path / "train.jsonl"
    eval_ = tmp_path / "eval.jsonl"
    write_jsonl(records[: int(len(records) * 0.8)], train)
    write_jsonl(records[int(len(records) * 0.8) :], eval_)
    return train, eval_


class TestBuildDataset:
    def test_streams_records(self, tmp_path: Path) -> None:
        path = tmp_path / "x.jsonl"
        write_jsonl(
            [
                {
                    "nl": "show students",
                    "candidate_kind": "entity",
                    "candidate_target": "users",
                    "relevance_label": 1.0,
                    "is_hard_negative": False,
                }
            ],
            path,
        )
        records = list(build_dataset(path))
        assert records[0]["candidate_target"] == "users"

    def test_missing_required_key_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.jsonl"
        path.write_text('{"nl": "x"}\n', encoding="utf-8")
        with pytest.raises(ValueError):
            list(build_dataset(path))

    def test_invalid_json_raises_with_lineno(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.jsonl"
        path.write_text("{not json}\n", encoding="utf-8")
        with pytest.raises(ValueError) as exc:
            list(build_dataset(path))
        assert "1:" in str(exc.value)


class TestPreflight:
    def test_passes_on_real_data(self, real_train_data: tuple[Path, Path], tmp_path: Path) -> None:
        train, eval_ = real_train_data
        cfg = LinkerTrainConfig(
            train_jsonl=train, eval_jsonl=eval_, output_dir=tmp_path / "out"
        )
        report = preflight(cfg)
        assert report.ok, f"unexpected issues: {report.issues}"
        assert report.train_count > 0
        assert report.eval_count > 0
        assert 0.0 < report.positive_fraction <= 1.0

    def test_flags_missing_train_file(self, tmp_path: Path) -> None:
        cfg = LinkerTrainConfig(
            train_jsonl=tmp_path / "missing.jsonl",
            eval_jsonl=tmp_path / "missing2.jsonl",
            output_dir=tmp_path / "out",
        )
        report = preflight(cfg)
        assert not report.ok
        assert any("train file missing" in i for i in report.issues)

    def test_flags_low_positive_fraction(self, tmp_path: Path) -> None:
        # 99 negatives + 1 positive
        records = [
            {
                "nl": "x",
                "candidate_kind": "field",
                "candidate_target": "users.x",
                "relevance_label": 0.0,
                "is_hard_negative": True,
            }
            for _ in range(99)
        ]
        records.append(
            {
                "nl": "y",
                "candidate_kind": "field",
                "candidate_target": "users.y",
                "relevance_label": 1.0,
                "is_hard_negative": False,
            }
        )
        train = tmp_path / "t.jsonl"
        eval_ = tmp_path / "e.jsonl"
        write_jsonl(records, train)
        write_jsonl(records[:1], eval_)
        cfg = LinkerTrainConfig(
            train_jsonl=train, eval_jsonl=eval_, output_dir=tmp_path / "out"
        )
        report = preflight(cfg)
        assert any("positive fraction" in i for i in report.issues)


class TestTrainLinker:
    def test_blocks_when_preflight_fails(self, tmp_path: Path) -> None:
        cfg = LinkerTrainConfig(
            train_jsonl=tmp_path / "missing.jsonl",
            eval_jsonl=tmp_path / "missing.jsonl",
            output_dir=tmp_path / "out",
        )
        with pytest.raises(RuntimeError, match="preflight failed"):
            train_linker(cfg)
