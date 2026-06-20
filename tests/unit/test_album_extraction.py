"""Unit tests for deterministic album extraction in services.parser.

Bug context: WXYC/request-o-matic#140 -- the Groq parser intermittently fails to
extract the album from "on <album>" / "from <album>" / "off <album>" /
"off of <album>" phrasings. The same input parses differently across runs
because the upstream LLM is nondeterministic at the cap we use it at.

The fix is a deterministic regex pre-pass that runs *before* Groq is invoked.
The regex covers the canonical templated shape and a denylist of common
"on X" idioms (radio / Friday / repeat / etc.) keeps the false-positive surface
small. These tests cover the pre-pass in isolation -- no Groq, no I/O.

Inputs come from the shared corpus ``tests.scenarios.ALBUM_PREPASS_CASES``; the
positive cases also drive the E2E smoke in tests/integration/test_integration.py
(negatives are unit-only -- see tests/scenarios.py for why E2E cannot verify a
declined input deterministically). The guard tests at the bottom of this file
keep the corpus's preposition *coverage* in lockstep with
``services.parser.SUPPORTED_ALBUM_PREPOSITIONS`` -- a new preposition branch
cannot ship without a positive case backing it. (Membership -- that a case names
only a supported preposition -- is enforced separately at type-check time by the
``Preposition`` Literal, not by a test here.)
"""

from __future__ import annotations

import re

import pytest

from services.parser import (
    _ALBUM_PREPOSITION_RE,
    SUPPORTED_ALBUM_PREPOSITIONS,
    extract_album_prefix,
)
from tests.scenarios import (
    ALBUM_PREPASS_CASES,
    ALBUM_PREPASS_NEGATIVES,
    ALBUM_PREPASS_POSITIVES,
)

POSITIVE_CASES = ALBUM_PREPASS_POSITIVES
NEGATIVE_CASES = ALBUM_PREPASS_NEGATIVES


# ---------------------------------------------------------------------------
# Positive cases: pre-pass should extract album and the suffix it consumed.
# ---------------------------------------------------------------------------
# `expected_stripped` is what the rest of the parser (Groq) should see after the
# pre-pass has consumed the trailing "{on|from|off|off of} <album>" clause. It
# still contains song + artist so Groq can extract those normally.


@pytest.mark.parametrize("case", POSITIVE_CASES, ids=[c.id for c in POSITIVE_CASES])
def test_extracts_album_from_canonical_phrasing(case) -> None:
    """The pre-pass extracts the album and returns the consumed-suffix-stripped message."""
    result = extract_album_prefix(case.raw_message)

    assert result is not None, f"Expected pre-pass to fire for: {case.raw_message!r}"
    extracted_album, stripped_message = result

    # Case-insensitive equality on the album (the regex preserves the listener's casing,
    # so we normalize before comparing). The stripped message must drop the suffix exactly.
    assert extracted_album.strip().lower() == case.expected_album.lower(), (
        f"album mismatch for {case.raw_message!r}: got {extracted_album!r}, "
        f"want {case.expected_album!r}"
    )
    assert stripped_message.strip() == case.expected_stripped.strip(), (
        f"stripped-message mismatch for {case.raw_message!r}: got {stripped_message!r}, "
        f"want {case.expected_stripped!r}"
    )


# ---------------------------------------------------------------------------
# Negative cases: pre-pass must NOT fire on common "on X" idioms / short forms.
# ---------------------------------------------------------------------------
# These leave album=null in the final parse. The regex is gated by a denylist
# of trailing tokens that are commonly idioms, plus a request-signal gate on
# `from` (so "hello from boston" is not mistaken for an album request).


@pytest.mark.parametrize("case", NEGATIVE_CASES, ids=[c.id for c in NEGATIVE_CASES])
def test_does_not_extract_album_for_idioms_or_short_forms(case) -> None:
    """The pre-pass returns None for inputs where the trailing clause is not an album."""
    assert extract_album_prefix(case.raw_message) is None, (
        f"Pre-pass incorrectly fired for: {case.raw_message!r}"
    )


def test_returns_none_for_messages_without_album_marker() -> None:
    """No "on" / "from" / "off" -> pre-pass declines to fire."""
    assert extract_album_prefix("la paradoja by juana molina") is None
    assert extract_album_prefix("Stereolab") is None
    assert extract_album_prefix("Spoonful-Cream-Wheels of Fire lp") is None


def test_returns_none_for_empty_input() -> None:
    """Defensive: empty input returns None instead of raising."""
    assert extract_album_prefix("") is None
    assert extract_album_prefix("   ") is None


# ---------------------------------------------------------------------------
# Enforcement: keep the shared corpus and the parser's preposition set in sync.
# ---------------------------------------------------------------------------
# These are the tripwires that make "heuristic parity" structural rather than a
# matter of vigilance. They run in normal CI (no external_api marker), so a
# preposition added to the parser without a shared corpus case -- which would
# silently skip the E2E layer that parametrises the same corpus -- fails here.


def test_every_supported_preposition_has_positive_corpus_case() -> None:
    """Every preposition the parser supports must have a positive shared case.

    The unit suite verifies every positive deterministically and the E2E suite
    smokes the new prepositions through the live path, both off this corpus, so a
    positive case per preposition guarantees the branch is covered. The label is
    trustworthy because ``test_positive_preposition_label_matches_regex`` asserts
    it equals what the regex actually matches.
    """
    covered = {c.preposition for c in POSITIVE_CASES}
    missing = set(SUPPORTED_ALBUM_PREPOSITIONS) - covered
    assert not missing, (
        "Prepositions supported by services.parser but with no positive case in "
        f"tests.scenarios.ALBUM_PREPASS_CASES (so no coverage): {sorted(missing)}. "
        "Add a positive AlbumPrepassCase for each."
    )


@pytest.mark.parametrize("case", POSITIVE_CASES, ids=[c.id for c in POSITIVE_CASES])
def test_positive_preposition_label_matches_regex(case) -> None:
    """The hand-entered ``preposition`` label must match what the regex matches.

    The coverage guard above keys off ``case.preposition``; if a case's label
    silently disagreed with its ``raw_message`` (a copy-paste typo), the guard
    could report a branch covered while a different branch was actually exercised.
    Anchor the label to the regex's own ``prep`` group so it cannot lie.
    """
    match = _ALBUM_PREPOSITION_RE.match(case.raw_message.strip())
    assert match is not None, (
        f"{case.id}: positive case raw_message does not match the pre-pass regex"
    )
    matched = re.sub(r"\s+", " ", match.group("prep").strip().lower())
    assert matched == case.preposition, (
        f"{case.id}: preposition label {case.preposition!r} disagrees with the regex match "
        f"{matched!r} for {case.raw_message!r}"
    )


# Membership ("every corpus preposition is one the parser supports") is enforced
# at type-check time by AlbumPrepassCase.preposition's Preposition Literal type,
# so it is not retested here. The guards below stay runtime because they evaluate
# the regex / inspect the corpus, which no type checker does.


def test_corpus_ids_are_unique() -> None:
    """Case ids must be unique -- they key the parametrize ids in both suites.

    The SearchScenario registry guards this via `_register`; the plain
    ALBUM_PREPASS_CASES list has no such guard, so a copy-pasted id would
    silently produce duplicate pytest node ids (and clobber any by-id lookup).
    """
    ids = [c.id for c in ALBUM_PREPASS_CASES]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    assert not duplicates, f"duplicate AlbumPrepassCase ids: {duplicates}"
