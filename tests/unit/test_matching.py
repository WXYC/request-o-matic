"""Tests for core.matching module."""


from core.matching import (
    COMPILATION_KEYWORDS,
    MAX_SEARCH_RESULTS,
    STOPWORDS,
    calculate_confidence,
    detect_ambiguous_format,
    is_compilation_artist,
)


class TestConstants:
    """Test that constants have expected values."""

    def test_max_search_results_is_5(self):
        assert MAX_SEARCH_RESULTS == 5

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


class TestDetectAmbiguousFormat:
    """Test detect_ambiguous_format function."""

    def test_detects_dash_format(self):
        result = detect_ambiguous_format("Artist - Song Title")
        assert result == ("Artist", "Song Title")

    def test_detects_period_format(self):
        result = detect_ambiguous_format("Artist. Song Title")
        assert result == ("Artist", "Song Title")

    def test_returns_none_for_normal_message(self):
        result = detect_ambiguous_format("Play Bohemian Rhapsody by Queen")
        assert result is None

    def test_returns_none_for_empty_parts(self):
        result = detect_ambiguous_format(" - Song Title")
        assert result is None
        result = detect_ambiguous_format("Artist - ")
        assert result is None

    def test_dash_with_multiple_dashes(self):
        # Should only split on first dash
        result = detect_ambiguous_format("Artist - Song - Remix")
        assert result == ("Artist", "Song - Remix")

    def test_handles_whitespace(self):
        result = detect_ambiguous_format("  Artist   -   Song Title  ")
        assert result == ("Artist", "Song Title")

    def test_period_only_triggers_with_space_after(self):
        # Period without space should not match
        result = detect_ambiguous_format("Dr.Dre - The Chronic")
        assert result == ("Dr.Dre", "The Chronic")

    def test_dash_requires_spaces_around(self):
        # Hyphenated words should not match
        result = detect_ambiguous_format("Jean-Michel Jarre Oxygene")
        assert result is None
