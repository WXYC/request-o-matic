"""Unit tests for refactored request handler helper functions."""

import pytest
from unittest.mock import AsyncMock, Mock

from routers.request import (
    build_context_message,
    detect_ambiguous_format,
    filter_results_by_artist,
    resolve_albums_for_track,
    search_library_with_fallback,
    search_with_alternative_interpretation,
)
from library.models import LibraryItem
from services.parser import MessageType, ParsedRequest


@pytest.fixture
def sample_request():
    """Create a sample parsed request."""
    return ParsedRequest(
        song="Bohemian Rhapsody",
        album=None,
        artist="Queen",
        is_request=True,
        message_type=MessageType.REQUEST,
        raw_message="Play Bohemian Rhapsody by Queen",
    )


def test_build_context_message_compilation():
    """Test context message for compilation match."""
    parsed = ParsedRequest(
        song="Test Song",
        artist="Test Artist",
        album=None,
        is_request=True,
        message_type=MessageType.REQUEST,
        raw_message="Test",
    )

    context = build_context_message(parsed, found_on_compilation=True, song_not_found=False)
    assert context == 'Found "Test Song" by Test Artist on:'


def test_build_context_message_album_not_found():
    """Test context message when album not found."""
    parsed = ParsedRequest(
        song="Test Song",
        artist="Test Artist",
        album="Test Album",
        is_request=True,
        message_type=MessageType.REQUEST,
        raw_message="Test",
    )

    context = build_context_message(parsed, found_on_compilation=False, song_not_found=True)
    assert context is not None
    assert "not found in the library" in context
    assert "Test Artist" in context


def test_build_context_message_song_not_found():
    """Test context message when song not found."""
    parsed = ParsedRequest(
        song="Test Song",
        artist="Test Artist",
        album=None,
        is_request=True,
        message_type=MessageType.REQUEST,
        raw_message="Test",
    )

    context = build_context_message(parsed, found_on_compilation=False, song_not_found=True)
    assert context is not None
    assert "is not on any album" in context
    assert "Test Artist" in context


def test_build_context_message_none():
    """Test that context is None when nothing special to report."""
    parsed = ParsedRequest(
        song="Test Song",
        artist="Test Artist",
        album="Test Album",
        is_request=True,
        message_type=MessageType.REQUEST,
        raw_message="Test",
    )

    context = build_context_message(parsed, found_on_compilation=False, song_not_found=False)
    assert context is None


def test_build_context_message_no_results():
    """Test context message when song not found AND no results to show."""
    parsed = ParsedRequest(
        song="Test Song",
        artist="Test Artist",
        album=None,
        is_request=True,
        message_type=MessageType.REQUEST,
        raw_message="Test",
    )

    context = build_context_message(
        parsed, found_on_compilation=False, song_not_found=True, has_results=False
    )
    assert context is not None
    assert "not found in library" in context
    assert "Test Song" in context
    assert "Test Artist" in context


def test_build_context_message_song_not_found_with_results():
    """Test context message when song not found but we have other albums to show."""
    parsed = ParsedRequest(
        song="Test Song",
        artist="Test Artist",
        album=None,
        is_request=True,
        message_type=MessageType.REQUEST,
        raw_message="Test",
    )

    context = build_context_message(
        parsed, found_on_compilation=False, song_not_found=True, has_results=True
    )
    assert context is not None
    assert "here are some albums" in context
    assert "Test Artist" in context


@pytest.mark.asyncio
async def test_search_library_with_fallback_full_query(mock_library_db):
    """Test library search with full query."""
    from library.models import LibraryItem

    mock_results = [
        LibraryItem(
            id=1,
            artist="Queen",
            title="A Night at the Opera",
            call_letters="Q",
            artist_call_number=1,
            release_call_number=1,
            genre="Rock",
            format="CD",
        )
    ]
    mock_library_db.search.return_value = mock_results

    parsed = ParsedRequest(
        song="Bohemian Rhapsody",
        artist="Queen",
        album="A Night at the Opera",
        is_request=True,
        message_type=MessageType.REQUEST,
        raw_message="Test",
    )

    results, fallback_used = await search_library_with_fallback(
        mock_library_db, parsed, ["A Night at the Opera"]
    )

    assert len(results) == 1
    assert results[0].artist == "Queen"
    assert fallback_used is False


@pytest.mark.asyncio
async def test_search_library_with_fallback_artist_only(mock_library_db):
    """Test library search falling back to artist only."""
    # First call returns empty, second (artist+song) returns empty, third returns results
    mock_library_db.search.side_effect = [
        [],  # First search with artist+album
        [],  # Second search with artist+song
        [
            LibraryItem(
                id=2,
                artist="Queen",
                title="The Game",
                call_letters="Q",
                artist_call_number=1,
                release_call_number=2,
                genre="Rock",
                format="CD",
            )
        ],  # Third search with artist only
    ]

    parsed = ParsedRequest(
        song="Test Song",
        artist="Queen",
        album="Unknown Album",
        is_request=True,
        message_type=MessageType.REQUEST,
        raw_message="Test",
    )

    results, fallback_used = await search_library_with_fallback(
        mock_library_db, parsed, ["Unknown Album"]
    )

    assert len(results) == 1
    assert results[0].artist == "Queen"
    assert fallback_used is True
    assert mock_library_db.search.call_count == 3


@pytest.mark.asyncio
async def test_search_library_with_fallback_filters_by_album_title(mock_library_db):
    """Test that fuzzy search results are filtered to match album title.

    Regression test for bug where searching for a track on "Wireless" by Biosphere
    would also return "Stator" because the fuzzy search matched the artist but not album.
    """
    # Mock returns both albums by the artist, but only one matches the album title
    mock_library_db.search.return_value = [
        LibraryItem(
            id=1,
            artist="Biosphere",
            title="Wireless",  # Matches the Discogs album
            call_letters="B",
            artist_call_number=1,
            release_call_number=1,
            genre="Electronic",
            format="CD",
        ),
        LibraryItem(
            id=2,
            artist="Biosphere",
            title="Stator",  # Does NOT match - should be filtered out
            call_letters="B",
            artist_call_number=1,
            release_call_number=2,
            genre="Electronic",
            format="CD",
        ),
    ]

    parsed = ParsedRequest(
        song="The Things I Tell You",
        artist="Biosphere",
        album=None,
        is_request=True,
        message_type=MessageType.REQUEST,
        raw_message="The Things I Tell You by Biosphere",
    )

    # Search with album name from Discogs
    results, fallback_used = await search_library_with_fallback(
        mock_library_db, parsed, ["Wireless - Live At The Arnolfini, Bristol"]
    )

    # Only Wireless should be returned, not Stator
    assert len(results) == 1
    assert results[0].title == "Wireless"
    assert fallback_used is False


# Tests for filter_results_by_artist
class TestFilterResultsByArtist:
    """Tests for the filter_results_by_artist function."""

    def test_filters_out_non_matching_artists(self):
        """Test that results not matching the artist are filtered out."""
        results = [
            LibraryItem(id=1, artist="Biz Markie", title="Young Girl Bluez"),
            LibraryItem(id=2, artist="Young Black Teenagers", title="Proud to be Black"),
            LibraryItem(id=3, artist="Young Gov", title="Some Album"),
        ]

        filtered = filter_results_by_artist(results, "Young Gov")

        assert len(filtered) == 1
        assert filtered[0].artist == "Young Gov"

    def test_keeps_matching_artists(self):
        """Test that results matching the artist are kept."""
        results = [
            LibraryItem(id=1, artist="Radiohead", title="OK Computer"),
            LibraryItem(id=2, artist="Radiohead", title="The Bends"),
            LibraryItem(id=3, artist="Radiohead", title="Kid A"),
        ]

        filtered = filter_results_by_artist(results, "Radiohead")

        assert len(filtered) == 3

    def test_case_insensitive_matching(self):
        """Test that artist matching is case insensitive."""
        results = [
            LibraryItem(id=1, artist="RADIOHEAD", title="OK Computer"),
            LibraryItem(id=2, artist="radiohead", title="The Bends"),
            LibraryItem(id=3, artist="Radiohead", title="Kid A"),
        ]

        filtered = filter_results_by_artist(results, "radiohead")

        assert len(filtered) == 3

    def test_partial_match_in_artist_field(self):
        """Test that partial matches work (artist name contained in field)."""
        results = [
            LibraryItem(id=1, artist="Various Artists - Rock - D", title="Disco Not Disco"),
            LibraryItem(id=2, artist="Queen", title="A Night at the Opera"),
        ]

        # "Various" should match "Various Artists - Rock - D"
        filtered = filter_results_by_artist(results, "Various")

        assert len(filtered) == 1
        assert filtered[0].artist is not None
        assert "Various" in filtered[0].artist

    def test_empty_results_returns_empty(self):
        """Test that empty input returns empty output."""
        results: list[LibraryItem] = []

        filtered = filter_results_by_artist(results, "Any Artist")

        assert len(filtered) == 0

    def test_no_artist_returns_all(self):
        """Test that empty/None artist returns all results unfiltered."""
        results = [
            LibraryItem(id=1, artist="Radiohead", title="OK Computer"),
            LibraryItem(id=2, artist="Queen", title="The Game"),
        ]

        filtered = filter_results_by_artist(results, "")
        assert len(filtered) == 2

        filtered = filter_results_by_artist(results, None)
        assert len(filtered) == 2

    def test_handles_none_artist_in_results(self):
        """Test that results with None artist are handled gracefully."""
        results = [
            LibraryItem(id=1, artist=None, title="Unknown Album"),
            LibraryItem(id=2, artist="Radiohead", title="OK Computer"),
        ]

        filtered = filter_results_by_artist(results, "Radiohead")

        assert len(filtered) == 1
        assert filtered[0].artist == "Radiohead"

    def test_young_gov_scenario(self):
        """Test the specific Young Gov scenario that was failing."""
        results = [
            LibraryItem(id=1, artist="Biz Markie", title='Young Girl Bluez 12"'),
            LibraryItem(id=2, artist="Young Black Teenagers", title='Proud to be Black 12"'),
            LibraryItem(id=3, artist="Young Black Teenagers", title="Young Black Teenagers"),
        ]

        filtered = filter_results_by_artist(results, "Young Gov")

        # None of these should match "Young Gov"
        assert len(filtered) == 0

    def test_laid_back_scenario(self):
        """Test the Laid Back scenario - should not match albums with 'laid back' in title."""
        results = [
            LibraryItem(
                id=1, artist="Various Artists - Hiphop", title="Night Shift - Laid Back Trip Hop"
            ),
            LibraryItem(id=2, artist="Gregg Allman", title="Laid Back"),
            LibraryItem(id=3, artist="Laid Back", title="Keep Smiling"),
        ]

        filtered = filter_results_by_artist(results, "Laid Back")

        # Only the actual band "Laid Back" should match
        assert len(filtered) == 1
        assert filtered[0].artist == "Laid Back"

    def test_amps_for_christ_scenario(self):
        """Test that 'Amps for Christ' matches correctly and filters out 'Edward Bear'."""
        results = [
            LibraryItem(id=1, artist="Edward Bear", title="Edward Bear"),
            LibraryItem(id=61692, artist="Amps for Christ", title="Circuits"),
        ]

        filtered = filter_results_by_artist(results, "Amps for Christ")

        # Only "Amps for Christ" should match, not "Edward Bear"
        assert len(filtered) == 1
        assert filtered[0].artist == "Amps for Christ"
        assert filtered[0].title == "Circuits"

    def test_toy_does_not_match_chew_toy(self):
        """Test that 'Toy' does not match 'Chew Toy' (word boundary matching)."""
        results = [
            LibraryItem(id=1, artist="Chew Toy", title="The Touch my Disney ep"),
            LibraryItem(id=2, artist="Toy", title="Toy"),
        ]

        filtered = filter_results_by_artist(results, "Toy")

        # Only "Toy" should match, not "Chew Toy"
        assert len(filtered) == 1
        assert filtered[0].artist == "Toy"
        assert filtered[0].title == "Toy"


@pytest.mark.asyncio
async def test_search_library_filters_non_matching_artists(mock_library_db):
    """Test that search_library_with_fallback filters out non-matching artists."""
    # Return results that don't match the searched artist
    mock_library_db.search.return_value = [
        LibraryItem(id=1, artist="Young Black Teenagers", title="Proud to be Black"),
        LibraryItem(id=2, artist="Biz Markie", title="Young Girl Bluez"),
    ]

    parsed = ParsedRequest(
        song="Some Song",
        artist="Young Gov",
        album=None,
        is_request=True,
        message_type=MessageType.REQUEST,
        raw_message="Test",
    )

    results, fallback_used = await search_library_with_fallback(mock_library_db, parsed, [])

    # Results should be empty after filtering
    assert len(results) == 0


# Tests for detect_ambiguous_format
class TestDetectAmbiguousFormat:
    """Tests for the detect_ambiguous_format function."""

    def test_detects_dash_format(self):
        """Test detection of 'X - Y' format."""
        result = detect_ambiguous_format("Amps for Christ - Edward")
        assert result == ("Amps for Christ", "Edward")

    def test_detects_period_format(self):
        """Test detection of 'X. Y' format."""
        result = detect_ambiguous_format("Amps for Christ. Edward")
        assert result == ("Amps for Christ", "Edward")

    def test_returns_none_for_normal_message(self):
        """Test that normal messages don't trigger ambiguity detection."""
        assert detect_ambiguous_format("play something by radiohead") is None

    def test_returns_none_for_empty_parts(self):
        """Test that empty parts don't match."""
        assert detect_ambiguous_format(" - Edward") is None
        assert detect_ambiguous_format("Artist - ") is None

    def test_dash_with_multiple_dashes(self):
        """Test that only the first dash is used as separator."""
        result = detect_ambiguous_format("Artist - Song - Remix")
        assert result == ("Artist", "Song - Remix")

    def test_handles_whitespace(self):
        """Test that extra whitespace is trimmed."""
        result = detect_ambiguous_format("  Artist  -  Title  ")
        assert result == ("Artist", "Title")


# Tests for search_with_alternative_interpretation
class TestSearchWithAlternativeInterpretation:
    """Tests for the search_with_alternative_interpretation function."""

    @pytest.mark.asyncio
    async def test_finds_correct_artist_first_interpretation(self, mock_library_db):
        """Test finding results when only first interpretation (part1=artist) matches."""
        # First search (part1 as artist) returns match
        # Second search (part2 as artist) returns no matching artist
        mock_library_db.search.side_effect = [
            [LibraryItem(id=1, artist="Amps for Christ", title="Circuits")],
            [LibraryItem(id=2, artist="Someone Else", title="Other Album")],
        ]

        results, _ = await search_with_alternative_interpretation(
            mock_library_db, "Amps for Christ", "Edward"
        )

        # Only Amps for Christ matches (Someone Else doesn't contain "Edward")
        assert len(results) == 1
        assert results[0].artist == "Amps for Christ"
        assert results[0].title == "Circuits"

    @pytest.mark.asyncio
    async def test_finds_correct_artist_second_interpretation(self, mock_library_db):
        """Test finding results when second interpretation (part2=artist) is correct."""
        # First search returns no matching artist
        # Second search returns match
        mock_library_db.search.side_effect = [
            [],
            [LibraryItem(id=1, artist="Queen", title="A Night at the Opera")],
        ]

        results, _ = await search_with_alternative_interpretation(
            mock_library_db, "Bohemian Rhapsody", "Queen"
        )

        assert len(results) == 1
        assert results[0].artist == "Queen"

    @pytest.mark.asyncio
    async def test_combines_results_from_both_interpretations(self, mock_library_db):
        """Test combining results when both interpretations find matches."""
        mock_library_db.search.side_effect = [
            [LibraryItem(id=1, artist="Artist A", title="Album 1")],
            [LibraryItem(id=2, artist="Artist B", title="Album 2")],
        ]

        results, _ = await search_with_alternative_interpretation(
            mock_library_db, "Artist A", "Artist B"
        )

        # Both results should be combined
        assert len(results) == 2
        ids = {r.id for r in results}
        assert ids == {1, 2}

    @pytest.mark.asyncio
    async def test_deduplicates_results(self, mock_library_db):
        """Test that duplicate results are removed."""
        same_item = LibraryItem(id=1, artist="Artist A", title="Album 1")
        mock_library_db.search.side_effect = [
            [same_item],
            [same_item],  # Same item returned by both interpretations
        ]

        results, _ = await search_with_alternative_interpretation(
            mock_library_db, "Artist A", "Something"
        )

        # Should only have one result despite appearing in both searches
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_matches(self, mock_library_db):
        """Test returning empty list when neither interpretation finds matches."""
        mock_library_db.search.side_effect = [
            [LibraryItem(id=1, artist="Wrong Artist", title="Album")],
            [LibraryItem(id=2, artist="Also Wrong", title="Another Album")],
        ]

        results, _ = await search_with_alternative_interpretation(
            mock_library_db, "Nonexistent Artist", "Unknown"
        )

        # After filtering by artist, should be empty
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_amps_for_christ_scenario(self, mock_library_db):
        """Test the specific 'Amps for Christ - Edward' scenario.

        This tests the ambiguous format where both interpretations might yield results.
        The function returns results from both interpretations so users can pick.
        """
        mock_library_db.search.side_effect = [
            # First query: "Amps for Christ Edward" - filtered by "Amps for Christ"
            [
                LibraryItem(id=61692, artist="Amps for Christ", title="Circuits"),
                LibraryItem(id=1, artist="Edward Bear", title="Edward Bear"),
            ],
            # Second query: "Edward Amps for Christ" - filtered by "Edward"
            [
                LibraryItem(id=1, artist="Edward Bear", title="Edward Bear"),
            ],
        ]

        results, _ = await search_with_alternative_interpretation(
            mock_library_db, "Amps for Christ", "Edward"
        )

        # Both interpretations find something:
        # - "Amps for Christ" matches Amps for Christ - Circuits
        # - "Edward" matches Edward Bear (because "edward" is in "edward bear")
        # Results are combined and deduplicated
        assert len(results) == 2
        artists = {r.artist for r in results}
        assert "Amps for Christ" in artists
        assert "Edward Bear" in artists
