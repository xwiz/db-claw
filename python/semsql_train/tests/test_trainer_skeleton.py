from __future__ import annotations

from pathlib import Path

import pytest

from semsql_train.generators import GeneratorConfig, generate_skeleton_pairs
from semsql_train.trainers.skeleton import (
    DistillationConfig,
    SkeletonTrainConfig,
    build_dataset,
    preflight,
    train_skeleton,
    write_jsonl,
)

from tests.fixtures.make_graph import build as build_graph


@pytest.fixture
def real_train_data(tmp_path: Path) -> tuple[Path, Path]:
    graph = build_graph(tmp_path / "g.semsql")
    cfg = GeneratorConfig(paraphrase_variants=2, seed=1)
    records = list(generate_skeleton_pairs(str(graph), cfg))
    train = tmp_path / "train.jsonl"
    eval_ = tmp_path / "eval.jsonl"
    split = max(int(len(records) * 0.8), 1)
    write_jsonl(records[:split], train)
    write_jsonl(records[split:] or records[:1], eval_)
    return train, eval_


def _good_record() -> dict:
    return {
        "nl": "show students",
        "natsql_skeleton": "SELECT * FROM @entity1",
        "ranked_schema": [{"kind": "entity", "target": "users", "score": 1.0}],
        "slot_map": {"@entity1": "users"},
    }


class TestBuildDataset:
    def test_streams_records(self, tmp_path: Path) -> None:
        path = tmp_path / "x.jsonl"
        write_jsonl([_good_record()], path)
        records = list(build_dataset(path))
        assert records[0]["natsql_skeleton"].startswith("SELECT")

    def test_missing_required_key_raises(self, tmp_path: Path) -> None:
        rec = _good_record()
        del rec["slot_map"]
        path = tmp_path / "bad.jsonl"
        write_jsonl([rec], path)
        with pytest.raises(ValueError, match="slot_map"):
            list(build_dataset(path))

    def test_empty_ranked_schema_raises(self, tmp_path: Path) -> None:
        rec = _good_record()
        rec["ranked_schema"] = []
        path = tmp_path / "bad.jsonl"
        write_jsonl([rec], path)
        with pytest.raises(ValueError, match="ranked_schema"):
            list(build_dataset(path))

    def test_stray_slot_map_keys_are_tolerated(self, tmp_path: Path) -> None:
        # Generator emits canonical slot_map entries even when paraphrase
        # collapses the placeholder out of the skeleton string. The
        # trainer must accept these — the cascade ranker only ever looks
        # up the slots that decoded.
        rec = _good_record()
        rec["slot_map"]["@val99"] = "ghost"
        path = tmp_path / "ok.jsonl"
        write_jsonl([rec], path)
        records = list(build_dataset(path))
        assert records[0]["slot_map"]["@val99"] == "ghost"


class TestPreflight:
    def test_passes_on_real_data(
        self, real_train_data: tuple[Path, Path], tmp_path: Path
    ) -> None:
        train, eval_ = real_train_data
        cfg = SkeletonTrainConfig(
            train_jsonl=train, eval_jsonl=eval_, output_dir=tmp_path / "out"
        )
        report = preflight(cfg)
        assert report.ok, f"unexpected issues: {report.issues}"
        assert report.train_count > 0

    def test_flags_missing_train_file(self, tmp_path: Path) -> None:
        cfg = SkeletonTrainConfig(
            train_jsonl=tmp_path / "missing.jsonl",
            eval_jsonl=tmp_path / "missing2.jsonl",
            output_dir=tmp_path / "out",
        )
        report = preflight(cfg)
        assert not report.ok
        assert any("train file missing" in i for i in report.issues)

    def test_flags_runaway_skeleton_length(self, tmp_path: Path) -> None:
        rec = _good_record()
        # 100 tokens of "@field1 ," — well above the 80-token cap.
        rec["natsql_skeleton"] = "SELECT " + ", ".join(["@field1"] * 100) + " FROM @entity1"
        rec["slot_map"] = {"@entity1": "users", "@field1": "users.x"}
        train = tmp_path / "train.jsonl"
        eval_ = tmp_path / "eval.jsonl"
        write_jsonl([rec], train)
        write_jsonl([rec], eval_)
        cfg = SkeletonTrainConfig(
            train_jsonl=train, eval_jsonl=eval_, output_dir=tmp_path / "out"
        )
        report = preflight(cfg)
        assert any("skeleton length" in i for i in report.issues)


class TestTrainSkeleton:
    def test_blocks_when_preflight_fails(self, tmp_path: Path) -> None:
        cfg = SkeletonTrainConfig(
            train_jsonl=tmp_path / "missing.jsonl",
            eval_jsonl=tmp_path / "missing.jsonl",
            output_dir=tmp_path / "out",
        )
        with pytest.raises(RuntimeError, match="preflight failed"):
            train_skeleton(cfg)


def _ml_extras_available() -> tuple[bool, str]:
    """True iff torch + transformers + accelerate can be imported AND the
    base model + tokenizer are fully cached locally. Network-only access is
    treated as unavailable so air-gapped CI runs deterministically.

    accelerate>=0.26.0 is a hard runtime requirement of HF Seq2SeqTrainer
    (it calls ``accelerate.state.AcceleratorState()`` inside
    ``Seq2SeqTrainingArguments.__post_init__``). Without it the trainer
    raises ImportError even when only one GPU / CPU epoch is requested.
    """
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
    except ImportError as e:
        return False, f"ML extras not installed: {e}"
    try:
        import accelerate  # noqa: F401
        from packaging.version import Version

        if Version(accelerate.__version__) < Version("0.26.0"):
            return False, f"accelerate {accelerate.__version__} < 0.26.0 required by HF Trainer"
    except ImportError:
        return False, "accelerate not installed (pip install 'accelerate>=0.26.0')"
    except Exception as e:  # noqa: BLE001
        return False, f"accelerate version check failed: {e}"
    try:
        from transformers import (
            AutoModelForSeq2SeqLM,
            AutoTokenizer,
        )

        # `local_files_only=True` makes the load fail iff anything is
        # missing — no implicit network round-trip.
        AutoTokenizer.from_pretrained("t5-small", local_files_only=True)
        AutoModelForSeq2SeqLM.from_pretrained("t5-small", local_files_only=True)
    except Exception as e:  # noqa: BLE001
        return False, f"t5-small not cached locally: {e}"
    return True, ""


_AVAILABLE, _REASON = _ml_extras_available()


@pytest.mark.skipif(not _AVAILABLE, reason=f"ML extras / model: {_REASON}")
class TestTrainSkeletonM1Smoke:
    """M1 acceptance: ``train_skeleton`` runs end-to-end on the
    synthetic mini-corpus. We cap ``max_steps`` and shrink the model
    via ``base_model='t5-small'`` so the smoke runs in a few seconds on
    CPU. Quality metrics live in `semsql_eval/per_stage.py`, not here."""

    def test_one_step_run_saves_model_to_output_dir(
        self, tmp_path: Path
    ) -> None:
        # Fabricate a 3-record corpus matching the generator's shape.
        records = [
            {
                "stage": 2,
                "nl": "show students",
                "ranked_schema": [
                    {"kind": "entity", "target": "users", "score": 1.0}
                ],
                "natsql_skeleton": "SELECT * FROM @entity1",
                "slot_map": {"@entity1": "users"},
            },
            {
                "stage": 2,
                "nl": "count active students",
                "ranked_schema": [
                    {"kind": "entity", "target": "users", "score": 1.0},
                    {
                        "kind": "field",
                        "target": "users.status_code",
                        "score": 0.9,
                    },
                ],
                "natsql_skeleton": (
                    "SELECT COUNT(*) FROM @entity1 WHERE @field1 = @val1"
                ),
                "slot_map": {
                    "@entity1": "users",
                    "@field1": "users.status_code",
                    "@val1": "2",
                },
            },
            {
                "stage": 2,
                "nl": "students with balance over 100",
                "ranked_schema": [
                    {"kind": "entity", "target": "users", "score": 1.0},
                    {"kind": "field", "target": "users.balance", "score": 0.95},
                ],
                "natsql_skeleton": (
                    "SELECT * FROM @entity1 WHERE @field1 > @val1"
                ),
                "slot_map": {
                    "@entity1": "users",
                    "@field1": "users.balance",
                    "@val1": "100",
                },
            },
        ]
        train = tmp_path / "train.jsonl"
        eval_ = tmp_path / "eval.jsonl"
        from semsql_train.trainers.skeleton import write_jsonl

        write_jsonl(records, train)
        write_jsonl(records[:1], eval_)

        cfg = SkeletonTrainConfig(
            train_jsonl=train,
            eval_jsonl=eval_,
            output_dir=tmp_path / "out",
            base_model="t5-small",
            epochs=1,
            batch_size=1,
            max_steps=1,
            max_source_tokens=64,
            max_target_tokens=32,
        )
        out = train_skeleton(cfg)

        # M1 acceptance: the output dir contains a saved model + tokenizer.
        assert out == cfg.output_dir
        assert (out / "config.json").exists()
        # Either pytorch_model.bin (legacy) or model.safetensors (post-4.30)
        # — accept either to keep the test robust across HF versions.
        weight_files = (
            list(out.glob("pytorch_model.bin"))
            + list(out.glob("model.safetensors"))
        )
        assert weight_files, f"no weights saved to {out}: {list(out.iterdir())}"
        # Tokenizer artefacts saved alongside.
        assert (out / "tokenizer_config.json").exists()


class TestDistillationConfig:
    """Defaults track `docs/stage2.md` §4.2; preflight tolerates the
    optional config; the config validates without torch installed."""

    def test_defaults_match_docs(self) -> None:
        kd = DistillationConfig(teacher_model="google/t5-efficient-base")
        assert kd.alpha == 0.5
        assert kd.beta == 0.3
        assert kd.gamma == 0.2
        assert kd.temperature == 2.0

    def test_skeleton_config_attaches_distillation(self, tmp_path: Path) -> None:
        kd = DistillationConfig(teacher_model="google/t5-efficient-base")
        cfg = SkeletonTrainConfig(
            train_jsonl=tmp_path / "t.jsonl",
            eval_jsonl=tmp_path / "e.jsonl",
            output_dir=tmp_path / "out",
            distillation=kd,
        )
        assert cfg.distillation is kd
        assert cfg.distillation.teacher_model == "google/t5-efficient-base"

    def test_scaled_up_preset_bumps_dimensions(self, tmp_path: Path) -> None:
        cfg = SkeletonTrainConfig.scaled_up(
            train_jsonl=tmp_path / "t.jsonl",
            eval_jsonl=tmp_path / "e.jsonl",
            output_dir=tmp_path / "out",
        )
        assert cfg.student_encoder_layers == 6
        assert cfg.student_decoder_layers == 6
        assert cfg.student_d_model == 512
        assert cfg.base_model == "google/t5-efficient-base"

    def test_laptop_accelerators_default_off(self, tmp_path: Path) -> None:
        cfg = SkeletonTrainConfig(
            train_jsonl=tmp_path / "t.jsonl",
            eval_jsonl=tmp_path / "e.jsonl",
            output_dir=tmp_path / "out",
        )
        # Defaults: every laptop accelerator is opt-in so CI is portable.
        assert cfg.bf16 is False
        assert cfg.flash_attention is None
        assert cfg.torch_compile is False
        assert cfg.liger_kernel is False

    def test_laptop_accelerators_opt_in(self, tmp_path: Path) -> None:
        cfg = SkeletonTrainConfig(
            train_jsonl=tmp_path / "t.jsonl",
            eval_jsonl=tmp_path / "e.jsonl",
            output_dir=tmp_path / "out",
            bf16=True,
            flash_attention="flash_attention_2",
            torch_compile=True,
            liger_kernel=True,
        )
        assert cfg.bf16
        assert cfg.flash_attention == "flash_attention_2"
        assert cfg.torch_compile
        assert cfg.liger_kernel

    def test_scaled_up_accepts_overrides(self, tmp_path: Path) -> None:
        cfg = SkeletonTrainConfig.scaled_up(
            train_jsonl=tmp_path / "t.jsonl",
            eval_jsonl=tmp_path / "e.jsonl",
            output_dir=tmp_path / "out",
            student_d_model=768,
            epochs=3,
        )
        assert cfg.student_d_model == 768
        assert cfg.epochs == 3

    def test_preflight_does_not_require_distillation(
        self, tmp_path: Path
    ) -> None:
        # Preflight is torch-free; the distillation config is only
        # consulted at training time. A config with distillation set must
        # still pass preflight on a valid corpus.
        rec = _good_record()
        train = tmp_path / "train.jsonl"
        eval_ = tmp_path / "eval.jsonl"
        write_jsonl([rec], train)
        write_jsonl([rec], eval_)

        cfg = SkeletonTrainConfig(
            train_jsonl=train,
            eval_jsonl=eval_,
            output_dir=tmp_path / "out",
            distillation=DistillationConfig(
                teacher_model="google/t5-efficient-base"
            ),
        )
        report = preflight(cfg)
        assert report.ok, f"issues: {report.issues}"


def test_format_source_renders_schema_block_and_question() -> None:
    """The `_format_source` helper is used by the dataset wrapper. Test
    here so the contract is locked even when ML extras aren't installed
    (the helper is plain Python — no torch dependency)."""
    from semsql_train.trainers.skeleton import _format_source

    rec = {
        "nl": "show active students",
        "ranked_schema": [
            {"kind": "entity", "target": "users", "score": 1.0},
            {"kind": "field", "target": "users.status_code", "score": 0.95},
            {"kind": "field", "target": "users.email", "score": 0.7},
        ],
    }
    src = _format_source(rec)
    assert src.startswith("question: show active students")
    assert "schema:" in src
    assert "users: status_code, email" in src


def test_format_source_handles_empty_schema() -> None:
    from semsql_train.trainers.skeleton import _format_source

    src = _format_source({"nl": "no schema query", "ranked_schema": []})
    assert "(empty)" in src


def test_format_source_renders_fk_lines() -> None:
    """Phase C: ``kind == "fk"`` entries become ``FK: a.x = b.y`` lines."""
    from semsql_train.trainers.skeleton import _format_source

    rec = {
        "nl": "students per school",
        "ranked_schema": [
            {"kind": "entity", "target": "schools", "score": 1.0},
            {"kind": "entity", "target": "satscores", "score": 1.0},
            {"kind": "field", "target": "schools.cdscode", "score": 1.0},
            {"kind": "field", "target": "satscores.cds", "score": 1.0},
            {"kind": "fk", "target": "schools.cdscode = satscores.cds", "score": 1.0},
        ],
    }
    src = _format_source(rec)
    assert "FK: schools.cdscode = satscores.cds" in src
    # FK lines are appended after the per-entity field lines.
    fk_pos = src.index("FK: schools")
    fields_pos = src.index("schools: cdscode")
    assert fields_pos < fk_pos


def test_format_source_dedupes_fk_lines() -> None:
    """Duplicate FK entries collapse to a single line, first-seen order."""
    from semsql_train.trainers.skeleton import _format_source

    rec = {
        "nl": "q",
        "ranked_schema": [
            {"kind": "entity", "target": "a"},
            {"kind": "fk", "target": "a.id = b.a_id"},
            {"kind": "fk", "target": "a.id = b.a_id"},
            {"kind": "fk", "target": "b.id = c.b_id"},
        ],
    }
    src = _format_source(rec)
    assert src.count("FK: a.id = b.a_id") == 1
    assert "FK: b.id = c.b_id" in src
    assert src.index("FK: a.id = b.a_id") < src.index("FK: b.id = c.b_id")


def test_format_source_length_probe_p99_under_130() -> None:
    """Length probe — even with full FK info appended, the encoder input
    must stay well under the 256-token max_source_tokens budget. The plan
    target is p99 <= 130 tokens (Phase C acceptance gate). We approximate
    tokens with whitespace splits; SentencePiece typically yields ~1.3x
    that count, so p99 whitespace-tokens <= 100 keeps SP <= 130 with
    margin.
    """
    from semsql_train.trainers.skeleton import _format_source

    # Realistic worst-case: 6 entities × ~8 fields each, plus 5 FK edges.
    entities = [f"e{i}" for i in range(6)]
    schema: list[dict] = [{"kind": "entity", "target": e} for e in entities]
    for e in entities:
        for j in range(8):
            schema.append({"kind": "field", "target": f"{e}.col_{j}"})
    fk_pairs = [
        ("e0", "id", "e1", "e0_id"),
        ("e1", "id", "e2", "e1_id"),
        ("e2", "id", "e3", "e2_id"),
        ("e3", "id", "e4", "e3_id"),
        ("e4", "id", "e5", "e4_id"),
    ]
    for a, af, b, bf in fk_pairs:
        schema.append({"kind": "fk", "target": f"{a}.{af} = {b}.{bf}"})
    rec = {
        "nl": "complex question over the cross-entity domain with several joined tables",
        "ranked_schema": schema,
    }
    src = _format_source(rec)
    tokens = src.split()
    assert len(tokens) <= 100, (
        f"encoder input whitespace-tokens={len(tokens)} exceeds 100 "
        f"(SP-token p99 budget = 130)"
    )
