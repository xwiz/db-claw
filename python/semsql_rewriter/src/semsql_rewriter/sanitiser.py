"""Vocabulary sanitiser — runs at extraction time.

Vocabulary fragments arrive from extractors that read developer-authored
files (Laravel lang/, Filament forms, i18n JSON, Blade templates). These
files are trusted to be code paths, but their *contents* are not signed
inputs — a typo or a copy-pasted snippet from the internet can introduce
something like ``'foo' => 'Active OR 1=1'``. We never want that string to
reach SQL.

Two distinct surfaces:

- **Canonical names** (entity names, field names, enum-value canonical IDs)
  must match ``[A-Za-z_][A-Za-z0-9_]{0,63}``. This is the strictest check;
  these are the names that get *quoted as identifiers* in the rendered SQL
  and so they must be rejection-safe.

- **Display labels** ("Joined Date", "Account Status") are never emitted
  into SQL — they only flow through the SemanticGraph as searchable
  vocabulary. They are still NFC-normalised, control-character-stripped,
  and length-capped, but the regex is permissive.

Anything that fails sanitisation goes to the merge engine's ``conflict_log``
with the failing string captured for review by ``semsql doctor``.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["SanitiserError", "sanitise_canonical_name", "sanitise_label"]

# Canonical-name allow-list. Mirrors the CanonicalName invariants in
# crates/semsql-core/src/ids.rs — keep the two in lock-step.
_CANONICAL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_MAX_LABEL_LEN = 256
_ZERO_WIDTH = {"​", "‌", "‍", "⁠", "﻿"}


class SanitiserError(ValueError):
    """A vocabulary fragment failed sanitisation."""


def sanitise_canonical_name(raw: str) -> str:
    """Validate a canonical name. Raises :class:`SanitiserError` on failure.

    A canonical name is any string that will be quoted as a SQL identifier
    by the dialect renderer. The allow-list is deliberately strict.
    """
    if not isinstance(raw, str):
        raise SanitiserError(f"canonical name must be str, got {type(raw).__name__}")
    if not _CANONICAL_RE.fullmatch(raw):
        raise SanitiserError(f"invalid canonical name: {raw!r}")
    return raw


def sanitise_label(raw: str) -> str:
    """Normalise a free-text display label.

    Labels never reach SQL — they live in the vocabulary index for matching.
    We still strip control + zero-width characters, NFC-normalise, and cap
    length to prevent abusive payloads from polluting the graph.
    """
    if not isinstance(raw, str):
        raise SanitiserError(f"label must be str, got {type(raw).__name__}")
    cleaned = unicodedata.normalize("NFC", raw)
    cleaned = "".join(
        ch for ch in cleaned if ch not in _ZERO_WIDTH and unicodedata.category(ch)[0] != "C"
    )
    cleaned = cleaned.strip()
    if not cleaned:
        raise SanitiserError("empty label after sanitisation")
    if len(cleaned) > _MAX_LABEL_LEN:
        raise SanitiserError(f"label exceeds {_MAX_LABEL_LEN} chars: {cleaned[:32]!r}…")
    return cleaned
