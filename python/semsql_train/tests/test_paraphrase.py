from __future__ import annotations

from semsql_train.paraphrase import ParaphraseConfig, paraphrase


class TestVerb:
    def test_first_variant_is_input_verbatim(self) -> None:
        out = paraphrase("show active students")
        assert out[0] == "show active students"

    def test_swaps_show_for_synonym(self) -> None:
        out = paraphrase("show active students", ParaphraseConfig(enabled=frozenset({"verb"})))
        joined = " | ".join(out).lower()
        assert any(tok in joined for tok in ("list", "find", "get", "display", "fetch"))

    def test_no_op_when_verb_absent(self) -> None:
        cfg = ParaphraseConfig(enabled=frozenset({"verb"}))
        out = paraphrase("count active students", cfg)
        # No 'show' to substitute → only the verbatim input.
        assert out == ["count active students"]


class TestQuantifier:
    def test_substitutes_over(self) -> None:
        out = paraphrase(
            "subscriptions over $50",
            ParaphraseConfig(enabled=frozenset({"quantifier"})),
        )
        joined = " | ".join(out).lower()
        assert "above" in joined or "more than" in joined or "greater than" in joined

    def test_no_double_substitution_within_word(self) -> None:
        cfg = ParaphraseConfig(enabled=frozenset({"quantifier"}))
        # 'overdraft' should not match the bare 'over' head.
        out = paraphrase("show overdraft accounts", cfg)
        for v in out:
            # Original 'overdraft' must remain intact in every variant.
            assert "overdraft" in v


class TestTemporal:
    def test_substitutes_last_2_weeks(self) -> None:
        out = paraphrase(
            "show students who joined last 2 weeks",
            ParaphraseConfig(enabled=frozenset({"temporal"})),
        )
        joined = " | ".join(out).lower()
        assert "past 14 days" in joined or "previous fortnight" in joined


class TestSubject:
    def test_remixes_active_students(self) -> None:
        out = paraphrase(
            "show active students",
            ParaphraseConfig(enabled=frozenset({"subject"})),
        )
        joined = " | ".join(out).lower()
        assert any(form in joined for form in ("who are active", "with active status"))


class TestDeterminism:
    def test_same_input_same_output(self) -> None:
        a = paraphrase("show active students who joined last 2 weeks")
        b = paraphrase("show active students who joined last 2 weeks")
        assert a == b


class TestCap:
    def test_max_variants_respected(self) -> None:
        out = paraphrase(
            "show active students",
            ParaphraseConfig(max_variants=3),
        )
        assert len(out) <= 3


class TestNoiseOptIn:
    def test_noise_disabled_by_default(self) -> None:
        # No 'noise' in default enabled set, so verbatim word should appear in
        # all variants.
        out = paraphrase("show active students who joined last 2 weeks")
        for v in out:
            assert "students" in v.lower() or "student" in v.lower()
