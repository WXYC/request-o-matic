"""Shared search scenarios for unit and integration tests.

Maintenance contract:
- When adding a new bug fix: create the scenario here, then add unit + integration
  tests, then update UNIT_COVERAGE and INTEGRATION_COVERAGE dicts below.
- When renaming a test: update the coverage dict entry.
- The meta-test (test_scenario_coverage.py) fails if a scenario is missing from
  either dict, making coverage gaps visible in CI.

Scenarios define INPUTS shared by both layers. Mock setup (unit) and assertions
(both layers) remain in the test files -- only artist/song/album/raw_message
are shared.
"""

from __future__ import annotations

from dataclasses import dataclass, field


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


# ---------------------------------------------------------------------------
# Coverage matrix
# ---------------------------------------------------------------------------
# Maps scenario ID -> list of test function paths that cover it.
# The meta-test verifies every scenario ID appears in both dicts.

UNIT_COVERAGE: dict[str, list[str]] = {
    "manu_dibango_compilation": [
        "tests/unit/test_request_flow.py::test_compilation_search_deduplication",
        "tests/unit/test_request_flow.py::test_compilation_search_returns_empty_when_no_song",
        "tests/unit/test_request_flow.py::test_build_context_message_for_found_compilation",
        "tests/unit/test_request_flow.py::test_compilation_found_replaces_artist_albums",
    ],
    "sugar_plant_false_positive": [],  # MISSING
    "plug_alias": [
        "tests/unit/test_library.py::TestFindSimilarArtist::test_short_name_not_corrected_to_similar",
        "tests/unit/test_request_flow.py::test_resolve_albums_accepts_alias_releases",
        "tests/unit/test_request_flow.py::test_filter_results_uses_alternate_artist_name",
    ],
    "sneaker_pimps_track_validation": [
        "tests/unit/test_request_flow.py::TestFilterResultsByTrackValidation::test_keeps_only_albums_with_track",
        "tests/unit/test_request_flow.py::TestFilterResultsByTrackValidation::test_returns_none_when_no_albums_validated",
        "tests/unit/test_request_flow.py::TestFilterResultsByTrackValidation::test_returns_none_when_no_discogs_service",
        "tests/unit/test_request_flow.py::TestFilterResultsByTrackValidation::test_returns_none_when_no_song",
        "tests/unit/test_request_flow.py::TestFilterResultsByTrackValidation::test_handles_discogs_search_returning_no_results",
        "tests/unit/test_request_flow.py::TestFilterResultsByTrackValidation::test_handles_discogs_exception_gracefully",
    ],
    "biosphere_album_filter": [
        "tests/unit/test_request_helpers.py::test_search_library_with_fallback_filters_by_album_title",
    ],
    "young_gov_prefix": [
        "tests/unit/test_request_helpers.py::TestFilterResultsByArtist::test_filters_out_non_matching_artists",
        "tests/unit/test_request_helpers.py::TestFilterResultsByArtist::test_young_gov_scenario",
    ],
    "laid_back_artist_vs_title": [
        "tests/unit/test_request_helpers.py::TestFilterResultsByArtist::test_laid_back_scenario",
    ],
    "toy_word_boundary": [
        "tests/unit/test_request_helpers.py::TestFilterResultsByArtist::test_toy_does_not_match_chew_toy",
    ],
    "amps_for_christ_ambiguous": [
        "tests/unit/test_request_helpers.py::TestFilterResultsByArtist::test_amps_for_christ_scenario",
        "tests/unit/test_request_helpers.py::TestSearchWithAlternativeInterpretation::test_amps_for_christ_scenario",
    ],
    "living_color_spelling": [
        "tests/unit/test_library.py::TestFindSimilarArtist::test_color_to_colour",
    ],
    "lush_track_filter": [
        "tests/unit/test_request_flow.py::test_compilation_filtering_rejects_wrong_artist_albums",
    ],
    "echo_bunnymen_artist_only": [],  # MISSING
    "aphex_twin_multiple_albums": [
        "tests/unit/test_request_flow.py::test_song_on_multiple_albums_returns_all",
        "tests/unit/test_request_flow.py::test_compilation_filtering_allows_soundtrack_when_discogs_says_various",
    ],
    "band_common_word": [
        "tests/unit/test_request_helpers.py::test_search_library_album_filter_excludes_single_common_word_matches",
    ],
    "meet_me_in_city": [],  # MISSING
    "mi_ami_comma_format": [],  # MISSING
    "holland_1945": [],  # MISSING
    "quixotic_special_chars": [],  # MISSING
    "eternal_hallucination": [],  # MISSING
}

INTEGRATION_COVERAGE: dict[str, list[str]] = {
    "manu_dibango_compilation": [
        "tests/integration/test_integration.py::TestDiscogsIntegration::test_manu_dibango_compilation_search",
        "tests/integration/test_integration.py::TestEndToEndIntegration::test_full_compilation_search_workflow",
    ],
    "sugar_plant_false_positive": [
        "tests/integration/test_integration.py::TestDiscogsIntegration::test_sugar_plant_filters_false_positive_compilations",
        "tests/integration/test_integration.py::TestFullRequestIntegration::test_sugar_plant_excludes_unrelated_compilations",
    ],
    "plug_alias": [
        "tests/integration/test_integration.py::TestFullRequestIntegration::test_plug_not_corrected_to_plugz",
        "tests/integration/test_integration.py::TestFullRequestIntegration::test_plug_me_and_mr_jones_finds_album",
    ],
    "sneaker_pimps_track_validation": [
        "tests/integration/test_integration.py::TestFullRequestIntegration::test_6_underground_sneaker_pimps_returns_only_becoming_x",
    ],
    "biosphere_album_filter": [
        "tests/integration/test_integration.py::TestFullRequestIntegration::test_biosphere_excludes_albums_without_track",
    ],
    "young_gov_prefix": [
        "tests/integration/test_integration.py::TestFullRequestIntegration::test_young_gov_excludes_young_black_teenagers",
    ],
    "laid_back_artist_vs_title": [
        "tests/integration/test_integration.py::TestFullRequestIntegration::test_laid_back_matches_band_not_album_titles",
    ],
    "toy_word_boundary": [
        "tests/integration/test_integration.py::TestFullRequestIntegration::test_toy_excludes_chew_toy",
    ],
    "amps_for_christ_ambiguous": [
        "tests/integration/test_integration.py::TestFullRequestIntegration::test_amps_for_christ_excludes_edward_bear",
    ],
    "living_color_spelling": [
        "tests/integration/test_integration.py::TestFullRequestIntegration::test_living_color_corrects_to_living_colour",
    ],
    "lush_track_filter": [
        "tests/integration/test_integration.py::TestFullRequestIntegration::test_thoughtforms_by_lush_excludes_albums_without_song",
    ],
    "echo_bunnymen_artist_only": [
        "tests/integration/test_integration.py::TestLibraryIntegration::test_artist_only_search_echo_and_the_bunnymen",
        "tests/integration/test_integration.py::TestFullRequestIntegration::test_artist_only_search_returns_results",
    ],
    "aphex_twin_multiple_albums": [],  # MISSING
    "band_common_word": [],  # MISSING
    "meet_me_in_city": [
        "tests/integration/test_integration.py::TestFullRequestIntegration::test_meet_me_in_the_city_returns_correct_album",
    ],
    "mi_ami_comma_format": [
        "tests/integration/test_integration.py::TestFullRequestIntegration::test_mi_ami_comma_format_returns_watersports",
    ],
    "holland_1945": [
        "tests/integration/test_integration.py::TestFullRequestIntegration::test_holland_1945_returns_aeroplane",
    ],
    "quixotic_special_chars": [
        "tests/integration/test_integration.py::TestParserIntegration::test_preserves_asterisks_in_artist_name",
    ],
    "eternal_hallucination": [
        "tests/integration/test_integration.py::TestParserIntegration::test_does_not_hallucinate_artist_names",
    ],
}
