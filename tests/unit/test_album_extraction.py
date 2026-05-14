"""Unit tests for deterministic album extraction in services.parser.

Bug context: WXYC/request-o-matic#140 -- the Groq parser intermittently fails to
extract the album from "on <album>" / "from <album>" / "off <album>" /
"off of <album>" phrasings. The same input parses differently across runs
because the upstream LLM is nondeterministic at the cap we use it at.

The fix is a deterministic regex pre-pass that runs *before* Groq is invoked.
The regex covers the canonical templated shape and a denylist of common
"on X" idioms (radio / Friday / repeat / etc.) keeps the false-positive surface
small. These tests cover the pre-pass in isolation -- no Groq, no I/O.
"""

from __future__ import annotations

import pytest

from services.parser import extract_album_prefix

# ---------------------------------------------------------------------------
# Positive cases: pre-pass should extract album and the suffix it consumed.
# ---------------------------------------------------------------------------
# Each case is (raw_message, expected_album, expected_stripped_message).
# `expected_stripped_message` is what the rest of the parser (Groq) should see
# after the pre-pass has consumed the trailing "{on|from|off|off of} <album>"
# clause. It still contains song + artist so Groq can extract those normally.

POSITIVE_CASES: list[tuple[str, str, str]] = [
    # "on <album>" -- canonical "on" preposition, WXYC artists.
    (
        "tower of dub by the orb on live '93",
        "live '93",
        "tower of dub by the orb",
    ),
    # "from <album>" -- "from" preposition.
    (
        "la paradoja by juana molina from doga",
        "doga",
        "la paradoja by juana molina",
    ),
    # "off <album>" -- "off" preposition, no "of".
    (
        "back, baby by jessica pratt off on your own love again",
        "on your own love again",
        "back, baby by jessica pratt",
    ),
    # "off of <album>" -- "off of" preposition.
    (
        "aluminum tunes by stereolab off of aluminum tunes",
        "aluminum tunes",
        "aluminum tunes by stereolab",
    ),
    # Bare "<song> off <album>" with no "by <artist>" -- still wins; the song
    # remains for Groq to extract.
    (
        "moon pix off moon pix",
        "moon pix",
        "moon pix",
    ),
    # Bare "<song> off of <album>".
    (
        "edits off of edits",
        "edits",
        "edits",
    ),
    # Mixed case + extra whitespace shouldn't matter.
    (
        "  Tower Of Dub by The Orb ON Live '93  ",
        "Live '93",
        "Tower Of Dub by The Orb",
    ),
    # Trailing politeness ("please", "thanks") is trimmed from the album text.
    (
        "tower of dub by the orb on live '93 please",
        "live '93",
        "tower of dub by the orb",
    ),
    (
        "la paradoja by juana molina from doga, thanks",
        "doga",
        "la paradoja by juana molina",
    ),
]


@pytest.mark.parametrize(("raw_message", "expected_album", "expected_stripped"), POSITIVE_CASES)
def test_extracts_album_from_canonical_phrasing(
    raw_message: str, expected_album: str, expected_stripped: str
) -> None:
    """The pre-pass extracts the album and returns the consumed-suffix-stripped message."""
    result = extract_album_prefix(raw_message)

    assert result is not None, f"Expected pre-pass to fire for: {raw_message!r}"
    extracted_album, stripped_message = result

    # Case-insensitive equality on the album (the regex preserves the listener's casing,
    # so we normalize before comparing). The stripped message must drop the suffix exactly.
    assert extracted_album.strip().lower() == expected_album.lower(), (
        f"album mismatch for {raw_message!r}: got {extracted_album!r}, want {expected_album!r}"
    )
    assert stripped_message.strip() == expected_stripped.strip(), (
        f"stripped-message mismatch for {raw_message!r}: got {stripped_message!r}, "
        f"want {expected_stripped!r}"
    )


# ---------------------------------------------------------------------------
# Negative cases: pre-pass must NOT fire on common "on X" idioms.
# ---------------------------------------------------------------------------
# These leave album=null in the final parse. The regex is gated by a denylist
# of trailing tokens that are commonly idioms, not album names.

NEGATIVE_CASES: list[str] = [
    # Canonical negatives from the ticket acceptance criteria.
    "moon pix by cat power on the radio",
    "moon pix by cat power on Friday",
    "moon pix by cat power on repeat",
    # Sibling idioms that fall in the same surface.
    "moon pix by cat power on air",
    "moon pix by cat power on vinyl",
    "moon pix by cat power on cd",
    # "off" as a request-style filler (off the top of my head etc.) -- without
    # a clear album token following, the pre-pass should not fire.
    "moon pix by cat power off the top of my head",
    # No "by <artist>" and the album side is empty -- not a templated shape.
    "moon pix off",
    "moon pix off of",
]


@pytest.mark.parametrize("raw_message", NEGATIVE_CASES)
def test_does_not_extract_album_for_idioms_or_short_forms(raw_message: str) -> None:
    """The pre-pass returns None for inputs where the trailing clause is not an album."""
    assert extract_album_prefix(raw_message) is None, (
        f"Pre-pass incorrectly fired for: {raw_message!r}"
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
