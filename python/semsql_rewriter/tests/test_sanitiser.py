from __future__ import annotations

import pytest

from semsql_rewriter import SanitiserError, sanitise_canonical_name, sanitise_label


class TestCanonical:
    @pytest.mark.parametrize("name", ["users", "tenant_id", "_private", "col42", "a"])
    def test_accepts_safe(self, name: str) -> None:
        assert sanitise_canonical_name(name) == name

    @pytest.mark.parametrize(
        "name",
        [
            "",
            "1users",
            "users-table",
            "active OR 1=1",
            "users; DROP",
            "users.col",  # caller must split before sanitising each segment
            "users ",
            "very_long_name_that_exceeds_sixty_four_characters_threshold_xxxxxxxxxxx",
        ],
    )
    def test_rejects_unsafe(self, name: str) -> None:
        with pytest.raises(SanitiserError):
            sanitise_canonical_name(name)

    def test_rejects_non_string(self) -> None:
        with pytest.raises(SanitiserError):
            sanitise_canonical_name(123)  # type: ignore[arg-type]


class TestLabel:
    def test_accepts_normal(self) -> None:
        assert sanitise_label("Joined Date") == "Joined Date"

    def test_strips_zero_width(self) -> None:
        assert sanitise_label("Stu​dents") == "Students"

    def test_nfc_normalises(self) -> None:
        composed = "café"
        decomposed = "café"
        assert sanitise_label(composed) == sanitise_label(decomposed)

    def test_rejects_empty(self) -> None:
        with pytest.raises(SanitiserError):
            sanitise_label("   ")

    def test_caps_length(self) -> None:
        with pytest.raises(SanitiserError):
            sanitise_label("a" * 1024)
