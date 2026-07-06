"""Shared test inputs for unit and integration tests.

Two corpora live here:

* ``SearchScenario`` / ``SCENARIOS`` -- search-request inputs. Scenarios define
  INPUTS; mock setup (unit) and assertions (both layers) stay in the test files.
  Only artist/song/album/raw_message are shared.
* ``AlbumPrepassCase`` / ``ALBUM_PREPASS_CASES`` -- album pre-pass inputs (see the
  block lower in this file). These additionally share ``expected_album``,
  ``expected_stripped`` and ``preposition`` (type-constrained to the parser's
  supported set via the ``Preposition`` Literal), and centralize the
  positive/negative partition. Coverage is intentionally asymmetric: negatives
  are unit-only and only the per-preposition positive smoke reaches the E2E suite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # `from __future__ import annotations` makes the `preposition` annotation
    # below a string, so Preposition only needs to be importable by the type
    # checker -- importing it for real would pull the Groq SDK into the corpus
    # module at collection time for no runtime benefit.
    from services.parser import Preposition


@dataclass(frozen=True)
class SearchScenario:
    """A search scenario shared across unit and integration test layers."""

    id: str
    description: str
    raw_message: str
    artist: str | None = None
    song: str | None = None
    album: str | None = None
    bug: str | None = None
    tags: frozenset[str] = field(default_factory=frozenset)
    xfail: bool = False
    xfail_reason: str | None = None


# ---------------------------------------------------------------------------
# Scenario registry
# ---------------------------------------------------------------------------

SCENARIOS: dict[str, SearchScenario] = {}


def _register(**kwargs) -> SearchScenario:
    s = SearchScenario(**kwargs)
    if s.id in SCENARIOS:
        raise ValueError(f"Duplicate scenario id: {s.id}")
    SCENARIOS[s.id] = s
    return s


# ---------------------------------------------------------------------------
# Scenario definitions
# ---------------------------------------------------------------------------

MANU_DIBANGO_COMPILATION = _register(
    id="manu_dibango_compilation",
    description="Compilation deduplication - Abele Dance on multiple Celluloid releases",
    raw_message="Abele dance (85 remix) by Manu Dibango",
    artist="Manu Dibango",
    song="Abele Dance",
    tags=frozenset({"compilation", "deduplication"}),
)

SUGAR_PLANT_FALSE_POSITIVE = _register(
    id="sugar_plant_false_positive",
    description="Filter false-positive compilations for 'Simple' by Sugar Plant",
    raw_message="Simple by Sugar Plant",
    artist="Sugar Plant",
    song="Simple",
    bug="Discogs returned '22 Explosive Hits' containing 'A Simple Man' by 'Sugar Bears'",
    tags=frozenset({"compilation", "false_positive"}),
)

PLUG_ALIAS = _register(
    id="plug_alias",
    description="Plug is a Discogs alias for Luke Vibert; avoid false correction to Plugz",
    raw_message="me and mr. jones by plug from drum n bass for papa",
    artist="Plug",
    song="Me And Mr Jones",
    album="Drum N Bass for Papa",
    bug="find_similar_artist falsely corrected Plug to Plugz at 89% similarity",
    tags=frozenset({"alias", "spelling_correction"}),
)

PLUG_COMMA_FORMAT = _register(
    id="plug_comma_format",
    description="'me and mr jones, plug' should parse Plug as artist, not substitute Loudon Wainwright III",
    raw_message="me and mr jones, plug",
    artist="Plug",
    song="Me And Mr Jones",
    bug="Parser substituted Loudon Wainwright III (a known performer of 'Me and Mr. Jones') instead of using the explicitly provided artist 'plug'",
    tags=frozenset({"parser", "artist_substitution", "comma_format"}),
)

# Same track as PLUG_ALIAS but WITHOUT the album, so the lookup can't ride the
# ARTIST_PLUS_ALBUM album-title path and must find the original via track search.
# The in-library original is "Drum 'n' Bass for Papa (+ Plug EPs 1,2 & 3)" (Discogs
# release 3192, WXYC library id 38167, cataloged under "Luke Vibert").
PLUG_NO_ALBUM = _register(
    id="plug_no_album",
    description=(
        "album-less 'me and mr. jones by plug' should surface the in-library original, "
        "not the non-library remix"
    ),
    raw_message="me and mr. jones by plug",
    artist="Plug",
    song="Me And Mr Jones",
    bug=(
        "Album-less track lookup surfaced non-library remix 'Me & Mr. Sutton' (release 1643641) "
        "row-less at the LML layer instead of in-library original 'Drum 'n' Bass for Papa' "
        "(library id 38167)"
    ),
    tags=frozenset({"alias", "track_search", "compilation"}),
)

SNEAKER_PIMPS_TRACK_VALIDATION = _register(
    id="sneaker_pimps_track_validation",
    description="6 Underground is on Becoming X but not Kiss & Swallow",
    raw_message="6 underground - sneaker pimps",
    artist="Sneaker Pimps",
    song="6 Underground",
    bug="Fallback returned all artist albums without filtering by track presence",
    tags=frozenset({"track_validation", "fallback"}),
)

BIOSPHERE_ALBUM_FILTER = _register(
    id="biosphere_album_filter",
    description="The Things I Tell You is on Wireless/Substrata, not Stator",
    raw_message="The Things I Tell You by Biosphere",
    artist="Biosphere",
    song="The Things I Tell You",
    bug="Fuzzy search matched artist but not album, returning Stator",
    tags=frozenset({"album_filter", "false_positive"}),
)

YOUNG_GOV_PREFIX = _register(
    id="young_gov_prefix",
    description="Young Gov should not match Young Black Teenagers",
    raw_message="Young Gov",
    artist="Young Gov",
    bug="Prefix matching was too loose, matching any artist starting with 'Young'",
    tags=frozenset({"artist_filter", "prefix_match"}),
)

LAID_BACK_ARTIST_VS_TITLE = _register(
    id="laid_back_artist_vs_title",
    description="Laid Back band vs albums with 'laid back' in title",
    raw_message="Laid Back",
    artist="Laid Back",
    bug="Search returned Various Artists compilations with 'laid back' only in title",
    tags=frozenset({"artist_filter", "title_confusion"}),
    xfail=True,
    xfail_reason=(
        "Known bug: artist-only searches may return Various Artists "
        "compilations with search term only in title"
    ),
)

TOY_WORD_BOUNDARY = _register(
    id="toy_word_boundary",
    description="Toy should not match Chew Toy (word boundary)",
    raw_message="Toy",
    artist="Toy",
    bug="Artist filtering matched partial words, so 'Toy' matched 'Chew Toy'",
    tags=frozenset({"artist_filter", "word_boundary"}),
)

AMPS_FOR_CHRIST_AMBIGUOUS = _register(
    id="amps_for_christ_ambiguous",
    description="Ambiguous 'Amps for Christ - Edward' format detection",
    raw_message="Amps for Christ - Edward",
    artist="Amps for Christ",
    song="Edward",
    bug="Ambiguous format detection incorrectly matched Edward Bear",
    tags=frozenset({"ambiguous_format", "artist_filter"}),
)

LIVING_COLOR_SPELLING = _register(
    id="living_color_spelling",
    description="Living Color (American) corrects to Living Colour (British)",
    raw_message="Cult of Personality by Living Color",
    artist="Living Color",
    song="Cult of Personality",
    bug="American spelling 'Living Color' didn't match British 'Living Colour'",
    tags=frozenset({"spelling_correction"}),
)

LUSH_TRACK_FILTER = _register(
    id="lush_track_filter",
    description="Thoughtforms by Lush excludes albums without the song",
    raw_message="Can i request thoughtforms by lush",
    artist="Lush",
    song="Thoughtforms",
    bug="Fallback returned all Lush albums including Lovelife which lacks the track",
    tags=frozenset({"track_validation", "fallback"}),
)

ECHO_BUNNYMEN_ARTIST_ONLY = _register(
    id="echo_bunnymen_artist_only",
    description="Artist-only search for Echo and the Bunnymen returns albums",
    raw_message="Can i request something from echo and the bunnymen",
    artist="Echo and the Bunnymen",
    bug="Artist-only searches returned no results after album support refactor",
    tags=frozenset({"artist_only", "regression"}),
)

APHEX_TWIN_MULTIPLE_ALBUMS = _register(
    id="aphex_twin_multiple_albums",
    description="Goon Gumpas on both Richard D. James Album and Morvern Callar",
    raw_message="Goon Gumpas by Aphex Twin",
    artist="Aphex Twin",
    song="Goon Gumpas",
    tags=frozenset({"compilation", "multiple_albums"}),
)

BAND_COMMON_WORD = _register(
    id="band_common_word",
    description="Chest Fever by The Band - album filter ignores single common words",
    raw_message="Chest Fever The Band",
    artist="The Band",
    song="Chest Fever",
    bug="Album 'The Band' matched Discogs 'Live Band # One' via shared word 'band'",
    tags=frozenset({"album_filter", "common_word"}),
)

MEET_ME_IN_CITY = _register(
    id="meet_me_in_city",
    description="Meet Me in the City by Junior Kimbrough returns correct album",
    raw_message="Meet Me in the City Junior Kimbrough",
    artist="Junior Kimbrough",
    song="Meet Me in the City",
    bug="Search returned 'Do the Rump' instead of the correct album",
    tags=frozenset({"song_match"}),
)

MI_AMI_COMMA_FORMAT = _register(
    id="mi_ami_comma_format",
    description="Comma-separated 'the man in your house, mi ami' parsed correctly",
    raw_message="the man in your house, mi ami",
    artist="Mi Ami",
    song="The Man in Your House",
    bug="Parser didn't recognize comma-separated format as a request",
    tags=frozenset({"parser", "comma_format"}),
)

HOLLAND_1945 = _register(
    id="holland_1945",
    description="Holland, 1945 by Neutral Milk Hotel returns Aeroplane Over the Sea",
    raw_message="Holland, 1945 Neutral Milk Hotel",
    artist="Neutral Milk Hotel",
    song="Holland, 1945",
    bug="Keyword search didn't prioritize albums with the song title",
    tags=frozenset({"song_match", "keyword_search"}),
    xfail=True,
    xfail_reason="Known bug: keyword search doesn't prioritize albums with the song title",
)

QUIXOTIC_SPECIAL_CHARS = _register(
    id="quixotic_special_chars",
    description="Parser preserves asterisks in Quix*o*tic",
    raw_message="something by quix*o*tic",
    artist="Quix*o*tic",
    bug="Parser normalized special characters, stripping asterisks",
    tags=frozenset({"parser", "special_chars"}),
)

ETERNAL_HALLUCINATION = _register(
    id="eternal_hallucination",
    description="Parser does not hallucinate artist names (Eternal, not Eternalux)",
    raw_message="mind odyssey by eternal",
    artist="Eternal",
    song="Mind Odyssey",
    bug="Parser hallucinated 'Eternalux' instead of extracting 'Eternal'",
    tags=frozenset({"parser", "hallucination"}),
)

SPOONFUL_DASH_FORMAT = _register(
    id="spoonful_dash_format",
    description="Dash-separated 'Spoonful-Cream-Wheels of Fire lp' parsed correctly",
    raw_message="Spoonful-Cream-Wheels of Fire lp",
    artist="Cream",
    song="Spoonful",
    album="Wheels of Fire",
    bug="Parser treated entire 'Spoonful-Cream-Wheels of Fire' as song and 'lp' as album",
    tags=frozenset({"parser", "dash_format"}),
)

SOME_PHIL_COLLINS_FILLER = _register(
    id="some_phil_collins_filler",
    description="'Some phil collins please' - 'some' is a filler word, not a song title",
    raw_message="Some phil collins please",
    artist="Phil Collins",
    bug="Parser interpreted 'Some' as a song title instead of recognizing it as a filler word",
    tags=frozenset({"parser", "filler_word"}),
)

SOMETHING_BY_HELDEN_FILLER = _register(
    id="something_by_helden_filler",
    description="'something by helden' - 'something' is a filler word, not a song title",
    raw_message="something by helden",
    artist="Helden",
    bug="Parser interpreted 'something' as a song title instead of recognizing it as a filler word",
    tags=frozenset({"parser", "filler_word"}),
)

MJ_LENDERMAN_BARE_ARTIST = _register(
    id="mj_lenderman_bare_artist",
    description="Bare artist name 'MJ Lenderman' should parse as artist, not song",
    raw_message="MJ Lenderman",
    artist="MJ Lenderman",
    bug="Parser classified bare artist name as song title instead of artist",
    tags=frozenset({"parser", "bare_name"}),
)

SARA_FLEETWOOD_MAC_GREETING = _register(
    id="sara_fleetwood_mac_greeting",
    description="Greeting 'Good Morning' is not a song title; 'Sarah from Fleetwood Mac' means song=Sara, artist=Fleetwood Mac",
    raw_message="Good Mirning i would live to hear Sarah from Fleetwod Mac",
    artist="Fleetwood Mac",
    song="Sara",
    bug="Parser treated greeting 'Good Morning' as song title and 'Sarah from Fleetwood Mac' as artist name",
    tags=frozenset({"parser", "greeting"}),
)

FLOW_COMA_808_STATE = _register(
    id="flow_coma_808_state",
    description="Flow Coma by 808 State should not match unrelated '808 State' album",
    raw_message="flow coma by 808 state",
    artist="808 State",
    song="Flow Coma",
    bug=(
        "search_album_fuzzy matched library album '808 State' to Discogs album "
        "'The Best Of 808 State: Blueprint' via token_set_ratio subset bias"
    ),
    tags=frozenset({"album_filter", "title_mismatch"}),
)

MONK_WELL_YOU_NEEDNT = _register(
    id="monk_well_you_neednt",
    description="'well, you needn't by thelonious monk' - 'well' is part of song title, not filler",
    raw_message="well, you needn't by thelonious monk",
    artist="Thelonious Monk",
    song="Well, You Needn't",
    bug="Parser treated 'well' as conversational filler and failed to extract the full song title",
    tags=frozenset({"parser", "filler_word", "song_title"}),
)

BECK_EDITORIAL_ALBUM = _register(
    id="beck_editorial_album",
    description="'Beck's best album: Stereopathic Soulmanuer' - editorial commentary is not the album title",
    raw_message="Beck's best album: Stereopathic Soulmanuer. It's AMAZING.",
    artist="Beck",
    album="Stereopathic Soulmanure",
    bug="Parser treated editorial phrase 'Beck's best album' as album title and the actual album name as song title",
    tags=frozenset({"parser", "editorial_commentary"}),
)

ORB_ON_ALBUM = _register(
    id="orb_on_album",
    description="'tower of dub by the orb on live '93' - 'on <album>' phrasing must populate album deterministically",
    raw_message="tower of dub by the orb on live '93",
    artist="The Orb",
    song="Tower of Dub",
    album="Live '93",
    bug="Groq parser populated album=null on 2 of 3 runs of this exact input -- LLM nondeterminism on the 'on <album>' templated shape",
    tags=frozenset({"parser", "album_preposition"}),
)

TODAY_JEFFERSON_AIRPLANE = _register(
    id="today_jefferson_airplane",
    description="'Today, Jefferson Airplane' comma shape: short common word on the left is the song, not a temporal preamble",
    raw_message="Today, Jefferson Airplane",
    artist="Jefferson Airplane",
    song="Today",
    bug="Parser dropped the song and put the artist in the song slot (song='Jefferson Airplane', artist=null) because it treated leading 'Today,' as a temporal aside",
    tags=frozenset({"parser", "comma_format"}),
)


# ---------------------------------------------------------------------------
# Album pre-pass corpus (services.parser.extract_album_prefix)
# ---------------------------------------------------------------------------
# Shared INPUTS for the deterministic album pre-pass. Coverage is asymmetric by
# design: the pre-pass is deterministic and `parse_request` *forces* the
# extracted album over Groq's (services/parser.py), so real-Groq E2E can only
# smoke the wiring -- it cannot verify behaviour the LLM does not influence.
#
#   * Positives (`expected_album` set): the pre-pass must fire.
#       - unit (test_album_extraction.py): asserts the regex returns
#         `expected_album` and `expected_stripped`. This is the real verification.
#       - E2E (test_integration.py): one representative case per *new* preposition
#         (from/off/off of) smokes the live parse_request path. The deterministic
#         overlay wiring is already covered with mocked Groq in
#         test_parser_album_overlay.py, and `on` by the 10x determinism test.
#   * Negatives (`expected_album` None): the pre-pass must decline. Unit-only and
#     deterministic -- asserting on real-Groq output for a declined input would
#     test Groq, not our denylist, and could flake. The decline path through
#     parse_request is covered with mocked Groq in
#     test_parser_album_overlay.py::test_no_overlay_when_pre_pass_declines.
#
# Membership -- that every case's `preposition` is one the parser supports -- is
# enforced at type-check time (the field is typed as services.parser.Preposition),
# not by a runtime test. The guard tests in test_album_extraction.py cover the
# rest: every entry of services.parser.SUPPORTED_ALBUM_PREPOSITIONS has a positive
# case, and each positive's `preposition` label matches what the regex actually
# matches (so the label can be trusted as the coverage key). A branch therefore
# cannot be added to the parser without shared coverage.


@dataclass(frozen=True)
class AlbumPrepassCase:
    """A single album pre-pass case shared across the unit and E2E layers.

    Positive case: ``expected_album`` is set; the pre-pass must fire.
    ``expected_stripped`` is the message the rest of the parser (Groq) should
    see after the trailing "{prep} <album>" clause is consumed.

    Negative case: ``expected_album`` is ``None``; the pre-pass must decline.
    Negatives are unit-only -- see the module comment above for why E2E cannot
    verify a declined input deterministically.

    ``preposition`` is the ``SUPPORTED_ALBUM_PREPOSITIONS`` entry the case
    exercises; its ``Preposition`` Literal type makes an unsupported value a mypy
    error, so membership is checked at type-check time rather than by a runtime
    guard. For positives a guard test also asserts the label equals what the regex
    matches (so it can be trusted as the coverage key); for negatives (which the
    regex may decline before reaching this preposition) it is an informational
    label, not regex-verified.
    """

    id: str
    raw_message: str
    preposition: Preposition
    expected_album: str | None = None
    expected_stripped: str | None = None

    @property
    def is_positive(self) -> bool:
        return self.expected_album is not None


ALBUM_PREPASS_CASES: list[AlbumPrepassCase] = [
    # -- Positives: pre-pass fires, album + stripped message returned. ---------
    AlbumPrepassCase(
        id="orb_on_live93",
        raw_message="tower of dub by the orb on live '93",
        preposition="on",
        expected_album="live '93",
        expected_stripped="tower of dub by the orb",
    ),
    AlbumPrepassCase(
        id="juana_from_doga",
        raw_message="la paradoja by juana molina from doga",
        preposition="from",
        expected_album="doga",
        expected_stripped="la paradoja by juana molina",
    ),
    AlbumPrepassCase(
        id="jessica_off_onyourown",
        raw_message="back, baby by jessica pratt off on your own love again",
        preposition="off",
        expected_album="on your own love again",
        expected_stripped="back, baby by jessica pratt",
    ),
    AlbumPrepassCase(
        id="stereolab_offof_aluminumtunes",
        raw_message="aluminum tunes by stereolab off of aluminum tunes",
        preposition="off of",
        expected_album="aluminum tunes",
        expected_stripped="aluminum tunes by stereolab",
    ),
    AlbumPrepassCase(
        id="moonpix_off_moonpix",
        raw_message="moon pix off moon pix",
        preposition="off",
        expected_album="moon pix",
        expected_stripped="moon pix",
    ),
    AlbumPrepassCase(
        id="edits_offof_edits",
        raw_message="edits off of edits",
        preposition="off of",
        expected_album="edits",
        expected_stripped="edits",
    ),
    AlbumPrepassCase(
        id="orb_on_live93_mixedcase",
        raw_message="  Tower Of Dub by The Orb ON Live '93  ",
        preposition="on",
        expected_album="Live '93",
        expected_stripped="Tower Of Dub by The Orb",
    ),
    AlbumPrepassCase(
        id="orb_on_live93_please",
        raw_message="tower of dub by the orb on live '93 please",
        preposition="on",
        expected_album="live '93",
        expected_stripped="tower of dub by the orb",
    ),
    AlbumPrepassCase(
        id="juana_from_doga_thanks",
        raw_message="la paradoja by juana molina from doga, thanks",
        preposition="from",
        expected_album="doga",
        expected_stripped="la paradoja by juana molina",
    ),
    # -- Negatives: pre-pass must decline (idioms, greetings, bare short-forms).
    # The id documents the tail that a false-fire would wrongly capture as album.
    AlbumPrepassCase(
        id="catpower_on_the_radio",
        raw_message="moon pix by cat power on the radio",
        preposition="on",
    ),
    AlbumPrepassCase(
        id="catpower_on_friday",
        raw_message="moon pix by cat power on Friday",
        preposition="on",
    ),
    AlbumPrepassCase(
        id="catpower_on_repeat",
        raw_message="moon pix by cat power on repeat",
        preposition="on",
    ),
    AlbumPrepassCase(
        id="catpower_on_air",
        raw_message="moon pix by cat power on air",
        preposition="on",
    ),
    AlbumPrepassCase(
        id="catpower_on_vinyl",
        raw_message="moon pix by cat power on vinyl",
        preposition="on",
    ),
    AlbumPrepassCase(
        id="catpower_on_cd",
        raw_message="moon pix by cat power on cd",
        preposition="on",
    ),
    AlbumPrepassCase(
        id="catpower_off_top_of_head",
        raw_message="moon pix by cat power off the top of my head",
        preposition="off",
    ),
    AlbumPrepassCase(
        id="moonpix_off_shortform",
        raw_message="moon pix off",
        preposition="off",
    ),
    AlbumPrepassCase(
        id="moonpix_offof_shortform",
        raw_message="moon pix off of",
        preposition="off of",
    ),
    AlbumPrepassCase(
        id="hello_from_boston",
        raw_message="hello from boston",
        preposition="from",
    ),
    AlbumPrepassCase(
        id="hi_from_newyork",
        raw_message="hi from new york",
        preposition="from",
    ),
    AlbumPrepassCase(
        id="greetings_from_chapelhill",
        raw_message="greetings from chapel hill",
        preposition="from",
    ),
    AlbumPrepassCase(
        id="calling_from_durham",
        raw_message="calling from durham",
        preposition="from",
    ),
]

# Partitioned once here so the unit and E2E suites import the same subsets rather
# than each re-deriving the `is_positive` split (which risks the layers drifting
# onto divergent subsets of the corpus).
ALBUM_PREPASS_POSITIVES: list[AlbumPrepassCase] = [c for c in ALBUM_PREPASS_CASES if c.is_positive]
ALBUM_PREPASS_NEGATIVES: list[AlbumPrepassCase] = [
    c for c in ALBUM_PREPASS_CASES if not c.is_positive
]
