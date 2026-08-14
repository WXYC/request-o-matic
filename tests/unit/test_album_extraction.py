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
    _split_quoted_album,
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


def test_leading_descriptor_strip_leaves_single_word_titles_intact() -> None:
    """A bare "album"/"record" title (no trailing text) must not be stripped away.

    The leading-descriptor strip requires trailing title text after the noun, so
    a real album literally titled "Album" (PiL) survives the "off album" phrasing.
    """
    result = extract_album_prefix("metal box by public image ltd off album")
    assert result is not None
    album, _ = result
    assert album.strip().lower() == "album"


def test_trailing_feedback_strip_preserves_politeness_word_inside_title() -> None:
    """A politeness word mid-title (no sentence boundary) is part of the title.

    The trailing-feedback trim only fires after a sentence boundary (terminal
    punctuation or a comma), so a single space before a politeness word -- as in
    an album whose title merely contains one -- does not truncate the title.
    """
    result = extract_album_prefix("some song by stereolab off pretty please goodbye")
    assert result is not None
    album, _ = result
    assert album.strip().lower() == "pretty please goodbye"


def test_trailing_feedback_strip_ignores_double_space_before_politeness_word() -> None:
    """A run of spaces is NOT a feedback boundary -- only punctuation/comma is.

    A double-space typo in a title ("pretty  please goodbye") must not be read as
    an appended feedback clause and truncated; a genuine feedback clause is
    introduced by punctuation or a comma instead.
    """
    result = extract_album_prefix("some song by stereolab off pretty  please goodbye")
    assert result is not None
    album, _ = result
    assert "please goodbye" in album.strip().lower()


def test_leading_descriptor_strip_preserves_titles_that_begin_with_the_noun() -> None:
    """Real titles beginning with the descriptor word keep their first word.

    "Album of the Year" and "Record Collection" are real albums; the strip fires
    only for the determiner-led form or a bare "album" not followed by "of", so
    the title-word usage survives.
    """
    # bare "album" + "of ..." is the title pattern, not a descriptor
    r1 = extract_album_prefix("some song by stereolab off album of the year")
    assert r1 is not None and r1[0].strip().lower() == "album of the year"
    # bare "record <word>" is left intact (only "the record" reads as a descriptor)
    r2 = extract_album_prefix("some song by cat power off record collection")
    assert r2 is not None and r2[0].strip().lower() == "record collection"


def test_leading_descriptor_strip_preserves_ep_lp_volume_titles() -> None:
    """ "EP 3" / "LP 2" volume-named releases: "ep"/"lp" are not descriptors."""
    r1 = extract_album_prefix("some track by chuquimamani-condori off ep 3")
    assert r1 is not None and r1[0].strip().lower() == "ep 3"
    r2 = extract_album_prefix("some track by juana molina off lp 2")
    assert r2 is not None and r2[0].strip().lower() == "lp 2"


def test_leading_descriptor_strip_fires_on_determiner_led_forms() -> None:
    """ "the album X" / "the record X" are unambiguous descriptors -> stripped."""
    r1 = extract_album_prefix("some song by cat power on the album moon pix")
    assert r1 is not None and r1[0].strip().lower() == "moon pix"
    r2 = extract_album_prefix("some song by stereolab off the record collection")
    assert r2 is not None and r2[0].strip().lower() == "collection"


def test_quoted_title_ends_at_the_closing_quote() -> None:
    """Quotation marks delimit the album; text after the closing quote is remainder.

    The listener's own quotes are an explicit end-of-title marker, which the
    heuristic trims cannot be: a closing quote followed by a run of spaces is not
    a sentence boundary, so an appended clause used to be swallowed whole into the
    album (WXYC/request-o-matic#261).
    """
    result = extract_album_prefix(
        'heavy rain by boris! off the album "noise"    thanks for your set, enjoying it!!'
    )
    assert result is not None
    album, remainder = result
    assert album == "noise"
    # The remainder is the whole message minus the album clause -- the tail is
    # rejoined to the prefix rather than dropped, so Groq still sees the aside.
    assert remainder == "heavy rain by boris! thanks for your set, enjoying it!!"


@pytest.mark.parametrize(
    ("raw_message", "expected_album"),
    [
        ('moon pix by cat power off the album "moon pix"', "moon pix"),
        ("moon pix by cat power off the album “moon pix”", "moon pix"),
        ("moon pix by cat power off the album 'moon pix'", "moon pix"),
        ("moon pix by cat power off the album ‘moon pix’", "moon pix"),
        # Mixed straight/curly pairing: Slack composers substitute one side only
        # often enough that requiring a matched pair would miss real requests.
        ('moon pix by cat power off the album “moon pix"', "moon pix"),
    ],
    ids=["straight-double", "curly-double", "straight-single", "curly-single", "mixed"],
)
def test_quoted_title_recognises_straight_and_curly_marks(
    raw_message: str, expected_album: str
) -> None:
    """Both straight and curly quote marks delimit a title, in either pairing."""
    result = extract_album_prefix(raw_message)
    assert result is not None
    assert result[0] == expected_album


def test_quoted_title_keeps_a_by_clause_that_is_title_text() -> None:
    """A "by" inside the quotes is title text, so the reverse-order guard stands down.

    Unquoted, "conspiracy on death by chocolate" declines because the album group
    cannot tell title text from an unclaimed "by <artist>" clause. Quotes settle
    it: whatever is inside them is the title.
    """
    result = extract_album_prefix('conspiracy on "death by chocolate"')
    assert result is not None
    album, remainder = result
    assert album == "death by chocolate"
    assert remainder == "conspiracy"


def test_quoted_title_hands_a_trailing_artist_clause_to_the_parser() -> None:
    """A "by <artist>" tail after the closing quote is rejoined, not dropped.

    The unquoted form of this shape declines outright so Groq can see the artist
    (see ``test_declines_when_album_tail_carries_an_unclaimed_artist_clause``).
    With quotes the album is unambiguous, so the pre-pass fires and passes the
    authorship clause through in the remainder.
    """
    result = extract_album_prefix('conspiracy on "neptune" by prince jammy')
    assert result is not None
    album, remainder = result
    assert album == "neptune"
    assert remainder == "conspiracy by prince jammy"


def test_quoted_title_overrides_the_idiom_denylist() -> None:
    """A quoted idiom-head is a title the listener disambiguated on purpose.

    "on vinyl" is an idiom and stays on the denylist unquoted; on "vinyl" quoted,
    the quotes are the listener telling us it is the title.
    """
    result = extract_album_prefix('some song by cat power on "vinyl"')
    assert result is not None
    assert result[0] == "vinyl"


def test_unbalanced_quote_falls_through_to_the_heuristic_path() -> None:
    """An opening mark with no closing mark is not a delimiter -- keep the heuristics.

    Titles that open with an apostrophe ("'Round Midnight", "'70s Music") would
    otherwise be read as an unterminated quotation.
    """
    result = extract_album_prefix("some song by cat power off 'round midnight")
    assert result is not None
    assert result[0] == "'round midnight"


def test_quoted_split_declines_a_whitespace_only_title() -> None:
    """Marks wrapping only whitespace are not a delimited title.

    ``extract_album_prefix`` rejects this shape earlier (see
    ``test_empty_quotes_decline``), so the helper is tested directly to pin the
    contract it is relied on for: a caller never gets an empty album back.
    """
    assert _split_quoted_album('"   "') is None


def test_empty_quotes_decline() -> None:
    """Quotes with nothing inside carry no album -- decline instead of forwarding "".

    Returning the bare quote characters as an album would send them to the lookup
    service as a real album filter and guarantee a miss.
    """
    assert extract_album_prefix('some song by cat power off ""') is None


def test_returns_none_for_messages_without_album_marker() -> None:
    """No "on" / "from" / "off" -> pre-pass declines to fire."""
    assert extract_album_prefix("la paradoja by juana molina") is None
    assert extract_album_prefix("Stereolab") is None
    assert extract_album_prefix("Spoonful-Cream-Wheels of Fire lp") is None


def test_declines_when_album_tail_carries_an_unclaimed_artist_clause() -> None:
    """Reverse-order "<song> {prep} <album> by <artist>" declines -> defers to Groq.

    The greedy album group swallows the trailing "by <artist>" authorship clause
    ("neptune by prince jammy"), which pollutes the album *and* starves Groq of the
    artist (it only ever sees the "conspiracy" prefix). An album title can
    legitimately contain "by" ("Death By Chocolate", "One By One"), so the pre-pass
    cannot safely split it here -- it declines whenever the prefix has not already
    named an artist, handing the whole message to Groq's world knowledge instead.
    """
    assert extract_album_prefix("conspiracy on neptune by prince jammy") is None
    # Same shape, but the "by" belongs to a real album title -- also declined, so
    # Groq (not the regex) decides album vs artist.
    assert extract_album_prefix("conspiracy on death by chocolate") is None


def test_still_fires_when_prefix_already_names_the_artist() -> None:
    """A "by <artist>" already in the prefix means the album's own "by" is title text.

    "moon pix by cat power off death by chocolate" already identifies the artist
    before the preposition, so the album's "by" ("Death By Chocolate") is part of
    the title, not an unclaimed artist -- the pre-pass keeps extracting the album.
    """
    result = extract_album_prefix("moon pix by cat power off death by chocolate")
    assert result is not None
    album, stripped = result
    assert album.strip().lower() == "death by chocolate"
    assert stripped.strip().lower() == "moon pix by cat power"


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
