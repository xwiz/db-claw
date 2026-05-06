"""Rule-based paraphrase rewriter.

Turns one NL→NatSQL pair into N variants by deterministic substitution.
Cheap, fully reproducible, no LLM in the loop. The cascade gets
paraphrase-robust by training on the variants — the cascade itself
doesn't paraphrase at inference.

Substitutions are *NL-only*. NatSQL is the ground truth and never
rewritten — paraphrase that touched the SQL would generate noise that
hurts training.

Supported categories (all opt-in via :class:`ParaphraseConfig.enabled`):

- ``verb``       — show / list / get / find / display / give me / fetch / pull up / return
- ``quantifier`` — over / above / more than / greater than / exceeding
- ``temporal``   — last 2 weeks / past 14 days / previous fortnight / over the last 2 weeks
- ``subject``    — "active students" / "students who are active" / "students with active status"
- ``noise``      — light typos (one char swap or doubling), capped at 1 typo per 8 words

Each call is deterministic given a seed; seed defaults to a hash of the
input NL so re-runs of the data generator produce stable training sets.
"""

from __future__ import annotations

import hashlib
import random
import re
from dataclasses import dataclass, field
from typing import Iterable

__all__ = [
    "ParaphraseConfig",
    "paraphrase",
]


# ---------------------------------------------------------------------------
# substitution tables
# ---------------------------------------------------------------------------

# Each entry maps a "head" (the *canonical* form we expect to see in the base
# template) to a list of synonyms that may replace it. The head must appear
# verbatim in the base NL — substitutions are token-aware (word boundaries),
# not embedded inside larger words.
_VERB_HEAD = "show"
_VERB_SYNS: tuple[str, ...] = (
    "show",
    "list",
    "get",
    "find",
    "display",
    "give me",
    "return",
    "fetch",
    "pull up",
)

_QUANTIFIER_PAIRS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("over", ("over", "above", "more than", "greater than", "exceeding")),
    ("under", ("under", "below", "less than", "fewer than")),
    ("at least", ("at least", "no fewer than", "minimum of")),
    ("at most", ("at most", "no more than", "maximum of")),
)

_TEMPORAL_PAIRS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "last 2 weeks",
        ("last 2 weeks", "past 14 days", "previous fortnight", "over the last 2 weeks"),
    ),
    (
        "last month",
        ("last month", "past 30 days", "in the previous month", "over the last month"),
    ),
    (
        "last year",
        ("last year", "past 12 months", "previous year", "over the last year"),
    ),
)

# Subject-pattern remix templates. Source pattern is `{adj} {noun_pl}` with
# an optional `who are`/`with status` reflow.
_SUBJECT_RX = re.compile(r"\b(active|inactive|new|stale)\s+([a-z]+s)\b")
_SUBJECT_FORMS: tuple[str, ...] = (
    "{adj} {noun}",
    "{noun} who are {adj}",
    "{noun} with {adj} status",
)


@dataclass
class ParaphraseConfig:
    """Knobs for the paraphraser."""

    enabled: frozenset[str] = field(
        default_factory=lambda: frozenset({"verb", "quantifier", "temporal", "subject"})
    )
    """Categories to apply. ``noise`` is opt-in (drops realism but adds
    typo robustness)."""

    max_variants: int = 4
    """Cap on number of variants returned per input. Variants past this cap
    are dropped (after dedup)."""

    seed: int | None = None
    """Override seed. ``None`` means hash the input NL — deterministic across
    runs but distinct per input."""


def paraphrase(nl: str, cfg: ParaphraseConfig | None = None) -> list[str]:
    """Yield paraphrase variants of ``nl``. Always includes the input verbatim
    as the first element so callers can blindly use ``[0]`` as the canonical."""
    cfg = cfg or ParaphraseConfig()
    seed = cfg.seed if cfg.seed is not None else _seed_from(nl)
    rng = random.Random(seed)

    variants: list[str] = [nl]
    seen: set[str] = {nl.lower()}
    budget = max(0, cfg.max_variants - 1)
    for v in _all_candidates(nl, cfg, rng):
        key = v.lower()
        if key in seen:
            continue
        seen.add(key)
        variants.append(v)
        if len(variants) - 1 >= budget:
            break
    return variants


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _seed_from(nl: str) -> int:
    return int.from_bytes(hashlib.sha256(nl.encode("utf-8")).digest()[:8], "big")


def _all_candidates(
    nl: str, cfg: ParaphraseConfig, rng: random.Random
) -> Iterable[str]:
    candidates: list[str] = []

    if "verb" in cfg.enabled:
        candidates.extend(_swap_verb(nl))
    if "quantifier" in cfg.enabled:
        candidates.extend(_swap_quantifier(nl))
    if "temporal" in cfg.enabled:
        candidates.extend(_swap_temporal(nl))
    if "subject" in cfg.enabled:
        candidates.extend(_remix_subject(nl))
    if "noise" in cfg.enabled:
        candidates.extend(_inject_noise(nl, rng))

    rng.shuffle(candidates)
    return candidates


def _word_boundary_replace(haystack: str, needle: str, replacement: str) -> str:
    pattern = re.compile(rf"\b{re.escape(needle)}\b", re.IGNORECASE)
    return pattern.sub(replacement, haystack, count=1)


def _swap_verb(nl: str) -> list[str]:
    out: list[str] = []
    for syn in _VERB_SYNS:
        if syn == _VERB_HEAD:
            continue
        if not re.search(rf"\b{_VERB_HEAD}\b", nl, flags=re.IGNORECASE):
            continue
        out.append(_word_boundary_replace(nl, _VERB_HEAD, syn))
    return out


def _swap_quantifier(nl: str) -> list[str]:
    out: list[str] = []
    for head, syns in _QUANTIFIER_PAIRS:
        if not re.search(rf"\b{re.escape(head)}\b", nl, flags=re.IGNORECASE):
            continue
        for syn in syns:
            if syn == head:
                continue
            out.append(_word_boundary_replace(nl, head, syn))
    return out


def _swap_temporal(nl: str) -> list[str]:
    out: list[str] = []
    for head, syns in _TEMPORAL_PAIRS:
        if head not in nl.lower():
            continue
        for syn in syns:
            if syn == head:
                continue
            # Case-insensitive replace, preserving the rest of the string.
            pattern = re.compile(re.escape(head), re.IGNORECASE)
            out.append(pattern.sub(syn, nl, count=1))
    return out


def _remix_subject(nl: str) -> list[str]:
    out: list[str] = []
    match = _SUBJECT_RX.search(nl)
    if not match:
        return out
    adj, noun = match.group(1), match.group(2)
    for form in _SUBJECT_FORMS:
        rendered = form.format(adj=adj, noun=noun)
        if rendered == match.group(0):
            continue
        out.append(_SUBJECT_RX.sub(rendered, nl, count=1))
    return out


def _inject_noise(nl: str, rng: random.Random) -> list[str]:
    """One typo per 8 words — char swap or doubled char. Skip 1-2 char words."""
    words = nl.split()
    if len(words) < 4:
        return []
    out: list[str] = []
    target = max(1, len(words) // 8)
    for _ in range(target):
        idx = rng.randrange(len(words))
        word = words[idx]
        if len(word) < 4 or not word.isalpha():
            continue
        op = rng.choice(("swap", "double"))
        if op == "swap":
            i = rng.randrange(len(word) - 1)
            mutated = word[:i] + word[i + 1] + word[i] + word[i + 2 :]
        else:
            i = rng.randrange(len(word))
            mutated = word[:i] + word[i] + word[i:]
        copy = list(words)
        copy[idx] = mutated
        out.append(" ".join(copy))
    return out
