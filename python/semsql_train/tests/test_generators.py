from __future__ import annotations

from pathlib import Path

import pytest

from semsql_train.generators import (
    GeneratorConfig,
    generate_e2e_pairs,
    generate_linker_pairs,
    generate_skeleton_pairs,
    generate_slot_pairs,
)

from tests.fixtures.make_graph import build as build_graph


@pytest.fixture
def graph(tmp_path: Path) -> str:
    return str(build_graph(tmp_path / "g.semsql"))


def _cfg(**overrides: object) -> GeneratorConfig:
    base: dict[str, object] = {"paraphrase_variants": 2, "seed": 1}
    base.update(overrides)
    return GeneratorConfig(**base)  # type: ignore[arg-type]


class TestLinker:
    def test_emits_records(self, graph: str) -> None:
        records = list(generate_linker_pairs(graph, _cfg()))
        assert records, "linker generator emitted nothing"

    def test_emits_positive_for_used_entity(self, graph: str) -> None:
        records = list(generate_linker_pairs(graph, _cfg()))
        assert any(
            r["candidate_kind"] == "entity"
            and r["candidate_target"] == "users"
            and r["relevance_label"] == 1.0
            for r in records
        )

    def test_emits_hard_negatives(self, graph: str) -> None:
        records = list(generate_linker_pairs(graph, _cfg()))
        # `tenants.created_at` should appear as a hard negative when the SQL
        # actually filters on `users.created_at`.
        assert any(
            r["candidate_target"] == "tenants.created_at" and r["is_hard_negative"]
            for r in records
        )


class TestSkeleton:
    def test_skeleton_replaces_canonical_names_with_placeholders(self, graph: str) -> None:
        records = list(generate_skeleton_pairs(graph, _cfg()))
        assert records
        for r in records:
            sk = r["natsql_skeleton"]
            # No raw entity canonical name leaks through.
            assert "users" not in sk or "@entity" in sk

    def test_skeleton_carries_slot_map(self, graph: str) -> None:
        records = list(generate_skeleton_pairs(graph, _cfg()))
        for r in records:
            assert "@entity1" in r["slot_map"]


class TestSlot:
    def test_correct_index_is_first_for_entity_slot(self, graph: str) -> None:
        records = [
            r for r in generate_slot_pairs(graph, _cfg()) if r["slot_name"] == "@entity1"
        ]
        assert records
        for r in records:
            assert r["candidates"][r["correct_index"]] == "users" or (
                r["candidates"][r["correct_index"]] == "tenants"
            )


class TestE2e:
    def test_emits_nl_and_natsql(self, graph: str) -> None:
        records = list(generate_e2e_pairs(graph, _cfg()))
        assert records
        for r in records:
            assert r["nl"]
            assert r["natsql"].startswith(("SELECT", "WITH"))


class TestVolume:
    def test_generator_produces_useful_volume(self, graph: str) -> None:
        # The fixture has 2 entities × ~6 fields × paraphrase variants and
        # operator combinations; we expect a few hundred records minimum so
        # the fine-tune harness has signal.
        records = list(generate_e2e_pairs(graph, _cfg()))
        assert len(records) > 50, f"only {len(records)} records — generator is sterile"


class TestDeterminism:
    def test_same_seed_same_output(self, graph: str) -> None:
        a = list(generate_e2e_pairs(graph, _cfg(seed=99)))
        b = list(generate_e2e_pairs(graph, _cfg(seed=99)))
        assert a == b
