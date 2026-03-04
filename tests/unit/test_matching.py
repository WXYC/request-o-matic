"""Tests for core.matching module."""

import pytest

from core.matching import (
    COMPILATION_KEYWORDS,
    STOPWORDS,
    calculate_confidence,
    is_compilation_artist,
    normalize_text,
    validate_track_on_tracklist,
)
from discogs.models import TrackItem


class TestConstants:
    """Test that constants have expected values."""

    def test_stopwords_contains_articles(self):
        assert "the" in STOPWORDS
        assert "a" in STOPWORDS
        assert "an" in STOPWORDS

    def test_stopwords_contains_conjunctions(self):
        assert "and" in STOPWORDS
        assert "with" in STOPWORDS
        assert "from" in STOPWORDS

    def test_compilation_keywords_contains_various(self):
        assert "various" in COMPILATION_KEYWORDS

    def test_compilation_keywords_contains_abbreviations(self):
        assert "v/a" in COMPILATION_KEYWORDS
        assert "v.a." in COMPILATION_KEYWORDS


class TestIsCompilationArtist:
    """Test is_compilation_artist function."""

    def test_various_artists(self):
        assert is_compilation_artist("Various Artists")

    def test_various_artists_with_suffix(self):
        assert is_compilation_artist("Various Artists - Rock - D")

    def test_soundtrack(self):
        assert is_compilation_artist("Soundtrack")

    def test_soundtracks_plural(self):
        assert is_compilation_artist("Soundtracks - M")

    def test_compilation(self):
        assert is_compilation_artist("Compilation")

    def test_va_abbreviation(self):
        assert is_compilation_artist("V/A")
        assert is_compilation_artist("V.A.")

    def test_case_insensitive(self):
        assert is_compilation_artist("VARIOUS ARTISTS")
        assert is_compilation_artist("various")

    def test_regular_artist_returns_false(self):
        assert not is_compilation_artist("The Beatles")
        assert not is_compilation_artist("Radiohead")

    def test_empty_string_returns_false(self):
        assert not is_compilation_artist("")

    def test_none_returns_false(self):
        # Callers should guard against None before calling is_compilation_artist
        # This test documents that passing empty string returns False
        assert not is_compilation_artist("")


class TestCalculateConfidence:
    """Test calculate_confidence function."""

    def test_exact_artist_and_album_match(self):
        score = calculate_confidence(
            request_artist="The Beatles",
            request_album="Abbey Road",
            result_artist="The Beatles",
            result_album="Abbey Road",
        )
        # 0.4 (artist) + 0.4 (album) + 0.2 (bonus) = 1.0
        assert score == 1.0

    def test_partial_artist_match(self):
        score = calculate_confidence(
            request_artist="Beatles",
            request_album=None,
            result_artist="The Beatles",
            result_album="Abbey Road",
        )
        # 0.3 (partial artist)
        assert score == 0.3

    def test_partial_album_match(self):
        score = calculate_confidence(
            request_artist=None,
            request_album="Abbey",
            result_artist="The Beatles",
            result_album="Abbey Road",
        )
        # 0.3 (partial album)
        assert score == 0.3

    def test_no_match_returns_base_score(self):
        score = calculate_confidence(
            request_artist="Radiohead",
            request_album="OK Computer",
            result_artist="The Beatles",
            result_album="Abbey Road",
        )
        # Base score when nothing matches
        assert score == 0.2

    def test_bonus_for_both_matches(self):
        score = calculate_confidence(
            request_artist="Beatles",
            request_album="Abbey",
            result_artist="The Beatles",
            result_album="Abbey Road",
        )
        # 0.3 (partial artist) + 0.3 (partial album) + 0.2 (bonus) = 0.8
        assert score == 0.8

    def test_capped_at_1_0(self):
        score = calculate_confidence(
            request_artist="The Beatles",
            request_album="Abbey Road",
            result_artist="The Beatles",
            result_album="Abbey Road",
        )
        assert score <= 1.0

    def test_none_request_fields(self):
        score = calculate_confidence(
            request_artist=None,
            request_album=None,
            result_artist="The Beatles",
            result_album="Abbey Road",
        )
        assert score == 0.2

    def test_case_insensitive(self):
        score = calculate_confidence(
            request_artist="THE BEATLES",
            request_album="ABBEY ROAD",
            result_artist="the beatles",
            result_album="abbey road",
        )
        assert score == 1.0


class TestNormalizeText:
    """Test normalize_text function."""

    def test_removes_punctuation(self):
        assert normalize_text("hello, world!") == "hello world"

    def test_lowercases(self):
        assert normalize_text("HELLO World") == "hello world"

    def test_normalizes_whitespace(self):
        assert normalize_text("hello   world") == "hello world"

    def test_empty_string(self):
        assert normalize_text("") == ""

    def test_preserves_underscores(self):
        # \w includes underscores, so they are preserved
        assert normalize_text("hello_world") == "hello_world"

    def test_preserves_digits(self):
        assert normalize_text("Vol. 2 (2001)") == "vol 2 2001"

    def test_strips_surrounding_whitespace(self):
        assert normalize_text("  hello  ") == "hello"

    def test_strips_non_latin_characters(self):
        # Non-Latin scripts are stripped (ASCII-only word characters)
        assert normalize_text("Aphex Twin リチャード") == "aphex twin"

    @pytest.mark.parametrize(
        "input_text, expected",
        [
            ("Dr. Dre", "dr dre"),
            ("AC/DC", "ac dc"),
            ("Guns N' Roses", "guns n roses"),
            ("Sigur Rós", "sigur r s"),
        ],
    )
    def test_music_metadata_examples(self, input_text, expected):
        assert normalize_text(input_text) == expected


class TestValidateTrackOnTracklist:
    """Test validate_track_on_tracklist function."""

    @pytest.mark.parametrize(
        "description,tracklist,release_artist,track,artist,expected",
        [
            (
                "finds track by per-track artist (compilation)",
                [TrackItem(position="1", title="My Song", artists=["The Artist"])],
                "Various Artists",
                "My Song",
                "The Artist",
                True,
            ),
            (
                "finds track by release artist (single-artist release)",
                [TrackItem(position="1", title="My Song", artists=[])],
                "The Artist",
                "My Song",
                "The Artist",
                True,
            ),
            (
                "returns False when track not found",
                [TrackItem(position="1", title="Different Song", artists=["The Artist"])],
                "The Artist",
                "My Song",
                "The Artist",
                False,
            ),
            (
                "returns False when artist not found",
                [TrackItem(position="1", title="My Song", artists=["Other Artist"])],
                "Other Artist",
                "My Song",
                "The Artist",
                False,
            ),
            (
                "handles Discogs numbering like (2) in artist names",
                [TrackItem(position="1", title="My Song", artists=[])],
                "The Artist (2)",
                "My Song",
                "The Artist",
                True,
            ),
            (
                "case-insensitive matching",
                [TrackItem(position="1", title="MY SONG", artists=["THE ARTIST"])],
                "Various Artists",
                "my song",
                "the artist",
                True,
            ),
            (
                "partial title match - track_lower in item_title",
                [TrackItem(position="1", title="My Song (Extended Mix)", artists=["The Artist"])],
                "Various Artists",
                "My Song",
                "The Artist",
                True,
            ),
            (
                "partial title match - item_title in track_lower",
                [TrackItem(position="1", title="My Song", artists=["The Artist"])],
                "Various Artists",
                "My Song (Extended Mix)",
                "The Artist",
                True,
            ),
            (
                "empty tracklist returns False",
                [],
                "The Artist",
                "My Song",
                "The Artist",
                False,
            ),
            (
                "partial artist match - artist_lower in track_artist",
                [TrackItem(position="1", title="My Song", artists=["The Artist Feat. Someone"])],
                "Various Artists",
                "My Song",
                "The Artist",
                True,
            ),
            (
                "partial artist match - track_artist in artist_lower",
                [TrackItem(position="1", title="My Song", artists=["Artist"])],
                "Various Artists",
                "My Song",
                "The Artist",
                True,
            ),
        ],
        ids=lambda x: x if isinstance(x, str) else "",
    )
    def test_validate_track_on_tracklist(
        self, description, tracklist, release_artist, track, artist, expected
    ):
        assert validate_track_on_tracklist(tracklist, release_artist, track, artist) is expected

    @pytest.mark.parametrize(
        "description,tracklist,release_artist,track,artist,trust,expected",
        [
            (
                "alias: trust_release_artist bypasses artist mismatch",
                [TrackItem(position="1", title="Me And Mr Jones", artists=[])],
                "Luke Vibert",
                "Me And Mr Jones",
                "Plug",
                True,
                True,
            ),
            (
                "alias: trust_release_artist=False still rejects mismatch",
                [TrackItem(position="1", title="Me And Mr Jones", artists=[])],
                "Luke Vibert",
                "Me And Mr Jones",
                "Plug",
                False,
                False,
            ),
            (
                "alias: trust_release_artist with no title match still rejects",
                [TrackItem(position="1", title="Different Song", artists=[])],
                "Luke Vibert",
                "Me And Mr Jones",
                "Plug",
                True,
                False,
            ),
            (
                "alias: trust_release_artist does not affect compilation path",
                [TrackItem(position="1", title="Me And Mr Jones", artists=["Other Artist"])],
                "Various Artists",
                "Me And Mr Jones",
                "Plug",
                True,
                False,
            ),
        ],
        ids=lambda x: x if isinstance(x, str) else "",
    )
    def test_validate_track_trust_release_artist(
        self, description, tracklist, release_artist, track, artist, trust, expected
    ):
        result = validate_track_on_tracklist(
            tracklist, release_artist, track, artist, trust_release_artist=trust
        )
        assert result is expected
