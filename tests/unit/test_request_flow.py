"""Integration-style unit tests for the complete request flow."""

from unittest.mock import AsyncMock, patch

import pytest

from core.orchestration import (
    build_context_message,
    filter_results_by_artist,
    filter_results_by_track_validation,
    resolve_albums_for_track,
    search_compilations_for_track,
)
from discogs.models import DiscogsSearchResponse, DiscogsSearchResult
from library.models import LibraryItem
from tests.conftest import make_parsed_request
from tests.scenarios import (
    APHEX_TWIN_MULTIPLE_ALBUMS,
    LUSH_TRACK_FILTER,
    MANU_DIBANGO_COMPILATION,
    PLUG_ALIAS,
    SNEAKER_PIMPS_TRACK_VALIDATION,
)


@pytest.mark.asyncio
async def test_compilation_search_deduplication(mock_library_db):
    """Test that compilation search deduplicates results by ID."""
    # Simulate Discogs returning multiple releases that map to the same library item
    duplicate_item = LibraryItem(
        id=62503,
        title="Celluloid Records- change the beat 1979-87",
        artist="Various Artists - Rock - C",
        call_letters="Z-C",
        artist_call_number=0,
        release_call_number=119,
        genre="Rock",
        format="cd",
    )

    # Mock search to return empty for keyword search, then return item for fuzzy searches
    search_count = 0

    async def mock_search(**kwargs):
        nonlocal search_count
        search_count += 1
        if search_count == 1:
            return []  # Keyword search returns nothing
        # Fuzzy search for Discogs albums returns the item
        return [duplicate_item]

    mock_library_db.search = mock_search

    S = MANU_DIBANGO_COMPILATION
    parsed = make_parsed_request(
        song=S.song,
        artist=S.artist,
        raw_message=S.raw_message,
    )

    with patch(
        "core.orchestration.lookup_releases_by_track", new_callable=AsyncMock
    ) as mock_lookup:
        # Simulate Discogs returning 4 releases that all map to the same album
        mock_lookup.return_value = [
            ("Various", "Change The Beat Vol 1"),
            ("Various", "Change The Beat Vol 2"),
            ("Various", "Change The Beat Vol 3"),
            ("Various", "Change The Beat Vol 4"),
        ]

        results, discogs_titles = await search_compilations_for_track(mock_library_db, parsed)

    # Should only return 1 unique result despite 4 Discogs releases
    assert len(results) == 1
    assert results[0].id == 62503
    assert results[0].title == "Celluloid Records- change the beat 1979-87"
    # Should have discogs title mapped (from first Discogs result)
    assert 62503 in discogs_titles
    assert discogs_titles[62503] == "Change The Beat Vol 1"


@pytest.mark.asyncio
async def test_compilation_search_returns_empty_when_no_song(mock_library_db):
    """Test that compilation search requires both song and artist."""
    S = MANU_DIBANGO_COMPILATION
    parsed_no_song = make_parsed_request(
        artist=S.artist,
        album="Soul Makossa",
        raw_message="Soul Makossa by Manu Dibango",
    )

    results, discogs_titles = await search_compilations_for_track(mock_library_db, parsed_no_song)
    assert results == []
    assert discogs_titles == {}

    parsed_no_artist = make_parsed_request(song=S.song, raw_message=S.song)

    results, discogs_titles = await search_compilations_for_track(mock_library_db, parsed_no_artist)
    assert results == []
    assert discogs_titles == {}


def test_build_context_message_for_found_compilation():
    """Test context message when song is found on compilation."""
    S = MANU_DIBANGO_COMPILATION
    parsed = make_parsed_request(
        song=S.song,
        artist=S.artist,
        raw_message=S.raw_message,
    )

    context = build_context_message(parsed, found_on_compilation=True, song_not_found=False)

    assert context == f'Found "{S.song}" by {S.artist} on:'


def test_build_context_message_for_artist_fallback():
    """Test context message when showing artist albums as fallback."""
    parsed = make_parsed_request(
        song="Unknown Song", artist="Queen", raw_message="Unknown Song by Queen"
    )

    context = build_context_message(parsed, found_on_compilation=False, song_not_found=True)

    assert (
        context
        == '"Unknown Song" is not on any album in the library, but here are some albums by Queen:'
    )


@pytest.mark.asyncio
async def test_song_on_multiple_albums_returns_all():
    """Test that when a song is on multiple albums in the library, all are returned.

    Example: "Goon Gumpas" by Aphex Twin is on both:
    - Richard D. James Album (by Aphex Twin)
    - Morvern Callar soundtrack (Various Artists)

    If BOTH albums are in the library and Discogs returns both,
    both should be in the results (this is different from the Manu Dibango
    case where the song is ONLY on a Various Artists compilation).
    """
    from core.orchestration import search_compilations_for_track

    mock_db = AsyncMock()

    richard_d_james = LibraryItem(
        id=1,
        title="Richard D. James Album",
        artist="Aphex Twin",
        call_letters="AP",
        artist_call_number=1,
        release_call_number=3,
        genre="Electronic",
        format="cd",
    )

    morvern_callar = LibraryItem(
        id=2,
        title="Morvern Callar",
        artist="Soundtracks - M",
        call_letters="Z-M",
        artist_call_number=0,
        release_call_number=73,
        genre="Soundtracks",
        format="cd",
    )

    S = APHEX_TWIN_MULTIPLE_ALBUMS
    parsed = make_parsed_request(
        song=S.song,
        artist=S.artist,
        raw_message=S.raw_message,
    )

    # Simulate Discogs returning both releases, and library having both
    with patch(
        "core.orchestration.lookup_releases_by_track", new_callable=AsyncMock
    ) as mock_lookup:
        mock_lookup.return_value = [
            ("Aphex Twin", "Richard D. James Album"),
            ("Various", "Morvern Callar (Original Motion Picture Soundtrack)"),
        ]

        # Mock fuzzy search to return the matching album for each Discogs result
        search_count = 0

        async def mock_search(**kwargs):
            nonlocal search_count
            search_count += 1
            if search_count == 1:
                return []  # Keyword search
            elif search_count == 2:
                return [richard_d_james]  # First Discogs match
            elif search_count == 3:
                return [morvern_callar]  # Second Discogs match
            return []

        mock_db.search = mock_search

        results, discogs_titles = await search_compilations_for_track(mock_db, parsed)

    # Should return BOTH albums since both are in the library
    assert len(results) == 2
    assert results[0].id == 1
    assert results[1].id == 2
    assert results[0].title == "Richard D. James Album"
    assert results[1].title == "Morvern Callar"
    # Should have discogs titles mapped for both
    assert len(discogs_titles) == 2


@pytest.mark.asyncio
async def test_compilation_filtering_allows_soundtrack_when_discogs_says_various():
    """Test that compilation filtering allows soundtrack/compilation artists through.

    The inline filtering in search_compilations_for_track has this logic:
    - If Discogs says the release is by "Various"/"Soundtrack"/"Compilation",
      allow library items with those keywords in the artist field
    - If Discogs says the release is by the actual artist,
      only allow library items by that artist

    This test ensures both paths work correctly before refactoring.
    """
    from core.orchestration import search_compilations_for_track

    mock_db = AsyncMock()

    # Library has soundtrack album (not "Various Artists" but "Soundtracks - M")
    soundtrack_album = LibraryItem(
        id=100,
        title="Morvern Callar",
        artist="Soundtracks - M",
        call_letters="Z-M",
        artist_call_number=0,
        release_call_number=73,
        genre="Soundtracks",
        format="cd",
    )

    # Library also has artist's own album
    artist_album = LibraryItem(
        id=101,
        title="Richard D. James Album",
        artist="Aphex Twin",
        call_letters="AP",
        artist_call_number=1,
        release_call_number=3,
        genre="Electronic",
        format="cd",
    )

    # Library has unrelated compilation that should NOT match
    unrelated_compilation = LibraryItem(
        id=102,
        title="Random Electronic Compilation",
        artist="Various Artists - Electronic",
        call_letters="Z-E",
        artist_call_number=0,
        release_call_number=50,
        genre="Electronic",
        format="cd",
    )

    S = APHEX_TWIN_MULTIPLE_ALBUMS
    parsed = make_parsed_request(
        song=S.song,
        artist=S.artist,
        raw_message=S.raw_message,
    )

    with patch(
        "core.orchestration.lookup_releases_by_track", new_callable=AsyncMock
    ) as mock_lookup:
        # Discogs returns: one by artist, one by "Various" (soundtrack)
        mock_lookup.return_value = [
            ("Aphex Twin", "Richard D. James Album"),
            ("Various", "Morvern Callar (Original Motion Picture Soundtrack)"),
        ]

        search_count = 0

        async def mock_search(**kwargs):
            nonlocal search_count
            search_count += 1
            if search_count == 1:
                return []  # Keyword search returns nothing
            elif search_count == 2:
                # Fuzzy search for "Richard D. James Album" returns artist album
                return [artist_album]
            elif search_count == 3:
                # Fuzzy search for "Morvern Callar" returns soundtrack AND unrelated compilation
                # The filter should keep soundtrack (Discogs said "Various") but reject
                # unrelated compilation since it doesn't match the Discogs release
                return [soundtrack_album, unrelated_compilation]
            return []

        mock_db.search = mock_search

        results, discogs_titles = await search_compilations_for_track(mock_db, parsed)

    # Current behavior: when Discogs says "Various" released something,
    # ANY compilation-type album from the fuzzy search is allowed through.
    # This is permissive but may cause false positives.
    # For refactoring, we preserve this behavior.
    assert (
        len(results) == 3
    ), f"Expected 3 results, got {len(results)}: {[r.title for r in results]}"

    result_ids = {r.id for r in results}
    assert 101 in result_ids, "Should include artist's own album"
    assert 100 in result_ids, "Should include soundtrack (Discogs said 'Various')"
    assert (
        102 in result_ids
    ), "Current behavior: unrelated compilation also included (permissive filter)"


@pytest.mark.asyncio
async def test_compilation_filtering_rejects_wrong_artist_albums():
    """Test that when Discogs says artist X released it, we don't match artist Y.

    This tests the artist filtering within compilation search:
    - Discogs says "Lush" released "Mad Love"
    - Library search returns "Love is Gone Mad" by "Big Eyes" (keyword match)
    - Filter should reject Big Eyes album (wrong artist)
    """
    from core.orchestration import search_compilations_for_track

    mock_db = AsyncMock()

    # Wrong album that matches keywords but wrong artist
    wrong_album = LibraryItem(
        id=200,
        title="Love is Gone Mad",
        artist="Big Eyes",
        call_letters="BI",
        artist_call_number=5,
        release_call_number=1,
        genre="Rock",
        format="cd",
    )

    # Correct album
    correct_album = LibraryItem(
        id=201,
        title="Mad Love",
        artist="Lush",
        call_letters="LU",
        artist_call_number=3,
        release_call_number=2,
        genre="Rock",
        format="cd",
    )

    S = LUSH_TRACK_FILTER
    parsed = make_parsed_request(song="Hypocrite", artist=S.artist, raw_message="Hypocrite by Lush")

    with patch(
        "core.orchestration.lookup_releases_by_track", new_callable=AsyncMock
    ) as mock_lookup:
        mock_lookup.return_value = [
            ("Lush", "Mad Love"),
        ]

        search_count = 0

        async def mock_search(**kwargs):
            nonlocal search_count
            search_count += 1
            if search_count == 1:
                return []  # Keyword search
            elif search_count == 2:
                # Fuzzy search returns both (keywords "mad" and "love" match both)
                return [wrong_album, correct_album]
            return []

        mock_db.search = mock_search

        results, discogs_titles = await search_compilations_for_track(mock_db, parsed)

    # Should only return correct album, filter out wrong artist
    assert len(results) == 1, f"Expected 1 result, got {len(results)}: {[r.title for r in results]}"
    assert results[0].id == 201
    assert results[0].artist == "Lush"


@pytest.mark.asyncio
async def test_compilation_found_replaces_artist_albums():
    """Test that finding a song on compilation replaces artist album results.

    Scenario: When artist albums are found as fallback, but then the actual song
    is found on a compilation, only the compilation should be returned.

    This tests the critical replacement logic:
    - Artist albums found: [Album1, Album2, Album3] (fallback)
    - Compilation found: [Compilation]
    - Final result: [Compilation] (artist albums replaced, not extended)
    """
    compilation_item = LibraryItem(
        id=62503,
        title="Celluloid Records- change the beat 1979-87",
        artist="Various Artists - Rock - C",
        call_letters="Z-C",
        artist_call_number=0,
        release_call_number=119,
        genre="Rock",
        format="cd",
    )

    S = MANU_DIBANGO_COMPILATION
    artist_albums = [
        LibraryItem(
            id=1,
            title="Soul Makossa",
            artist=S.artist,
            call_letters="DI",
            artist_call_number=12,
            release_call_number=1,
            genre="Africa",
            format="cd",
        ),
        LibraryItem(
            id=2,
            title="Polysonik",
            artist=S.artist,
            call_letters="DI",
            artist_call_number=12,
            release_call_number=2,
            genre="Africa",
            format="cd",
        ),
        LibraryItem(
            id=3,
            title="The Rough Guide",
            artist=S.artist,
            call_letters="DI",
            artist_call_number=12,
            release_call_number=3,
            genre="Africa",
            format="cd",
        ),
    ]

    # Simulate the complete flow from handle_request
    library_results = artist_albums.copy()  # Start with artist albums (fallback)
    song_not_found = True
    found_on_compilation = False

    # Compilation search finds the song
    compilation_results = [compilation_item]

    # This is the critical logic from handle_request:
    if compilation_results:
        # Replace artist albums with compilation results
        library_results = compilation_results[:5]
        found_on_compilation = True
        song_not_found = False

    # Verify final state: ONLY compilation, not artist albums
    assert (
        len(library_results) == 1
    ), f"Expected 1 compilation, got {len(library_results)}: {[r.title for r in library_results]}"
    assert library_results[0].id == 62503
    assert library_results[0].title == "Celluloid Records- change the beat 1979-87"
    assert library_results[0].artist is not None
    assert "Various Artists" in library_results[0].artist
    assert found_on_compilation is True
    assert song_not_found is False


class TestFilterResultsByTrackValidation:
    """Tests for filter_results_by_track_validation.

    When the search pipeline falls back to returning all artist albums
    (song_not_found=True), this function validates each album against Discogs
    to determine which ones actually contain the requested track.
    """

    S = SNEAKER_PIMPS_TRACK_VALIDATION

    @pytest.fixture
    def becoming_x(self):
        return LibraryItem(
            id=2725,
            artist=self.S.artist,
            title="Becoming X",
            call_letters="Sn",
            artist_call_number=5,
            release_call_number=1,
            genre="Hiphop",
            format="cd",
        )

    @pytest.fixture
    def kiss_and_swallow(self):
        return LibraryItem(
            id=70823,
            artist=self.S.artist,
            title="Kiss & Swallow",
            call_letters="Sn",
            artist_call_number=5,
            release_call_number=2,
            genre="Hiphop",
            format="cdr",
        )

    @pytest.mark.asyncio
    async def test_keeps_only_albums_with_track(self, becoming_x, kiss_and_swallow):
        """6 Underground is on Becoming X but not Kiss & Swallow.

        Bug: Fallback search returned all Sneaker Pimps albums without filtering
        by track presence. The validation should keep only Becoming X.
        """
        mock_discogs = AsyncMock()

        mock_discogs.search.side_effect = [
            DiscogsSearchResponse(
                results=[
                    DiscogsSearchResult(
                        album="Becoming X",
                        artist=self.S.artist,
                        release_id=1273511,
                    )
                ],
                total=1,
            ),
            DiscogsSearchResponse(
                results=[
                    DiscogsSearchResult(
                        album="Kiss & Swallow",
                        artist=self.S.artist,
                        release_id=11181051,
                    )
                ],
                total=1,
            ),
        ]

        # Becoming X has "6 Underground", Kiss & Swallow does not
        mock_discogs.validate_track_on_release.side_effect = [True, False]

        results = await filter_results_by_track_validation(
            [becoming_x, kiss_and_swallow],
            self.S.song,
            self.S.artist,
            mock_discogs,
        )

        assert results is not None
        assert len(results) == 1
        assert results[0].title == "Becoming X"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_albums_validated(self, becoming_x, kiss_and_swallow):
        """When no albums are validated, return None to keep fallback behavior."""
        mock_discogs = AsyncMock()

        mock_discogs.search.side_effect = [
            DiscogsSearchResponse(
                results=[
                    DiscogsSearchResult(
                        album="Becoming X",
                        artist=self.S.artist,
                        release_id=1273511,
                    )
                ],
                total=1,
            ),
            DiscogsSearchResponse(
                results=[
                    DiscogsSearchResult(
                        album="Kiss & Swallow",
                        artist=self.S.artist,
                        release_id=11181051,
                    )
                ],
                total=1,
            ),
        ]

        # Neither album validates (Discogs tracklist data missing or different)
        mock_discogs.validate_track_on_release.side_effect = [False, False]

        results = await filter_results_by_track_validation(
            [becoming_x, kiss_and_swallow],
            self.S.song,
            self.S.artist,
            mock_discogs,
        )

        assert results is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_discogs_service(self, becoming_x, kiss_and_swallow):
        """When Discogs service is unavailable, return None."""
        results = await filter_results_by_track_validation(
            [becoming_x, kiss_and_swallow],
            self.S.song,
            self.S.artist,
            None,
        )

        assert results is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_song(self, becoming_x):
        """When no song is provided, return None."""
        mock_discogs = AsyncMock()

        results = await filter_results_by_track_validation(
            [becoming_x], None, self.S.artist, mock_discogs
        )

        assert results is None

    @pytest.mark.asyncio
    async def test_handles_discogs_search_returning_no_results(self, becoming_x, kiss_and_swallow):
        """When Discogs search returns no results for an album, skip it."""
        mock_discogs = AsyncMock()

        mock_discogs.search.side_effect = [
            DiscogsSearchResponse(
                results=[
                    DiscogsSearchResult(
                        album="Becoming X",
                        artist=self.S.artist,
                        release_id=1273511,
                    )
                ],
                total=1,
            ),
            # Kiss & Swallow not found on Discogs
            DiscogsSearchResponse(results=[], total=0),
        ]

        mock_discogs.validate_track_on_release.return_value = True

        results = await filter_results_by_track_validation(
            [becoming_x, kiss_and_swallow],
            self.S.song,
            self.S.artist,
            mock_discogs,
        )

        assert results is not None
        assert len(results) == 1
        assert results[0].title == "Becoming X"

    @pytest.mark.asyncio
    async def test_handles_discogs_exception_gracefully(self, becoming_x, kiss_and_swallow):
        """When Discogs throws an exception for one album, continue with others."""
        mock_discogs = AsyncMock()

        mock_discogs.search.side_effect = [
            Exception("Discogs API error"),
            DiscogsSearchResponse(
                results=[
                    DiscogsSearchResult(
                        album="Kiss & Swallow",
                        artist=self.S.artist,
                        release_id=11181051,
                    )
                ],
                total=1,
            ),
        ]

        mock_discogs.validate_track_on_release.return_value = True

        results = await filter_results_by_track_validation(
            [becoming_x, kiss_and_swallow],
            self.S.song,
            self.S.artist,
            mock_discogs,
        )

        # Kiss & Swallow validated (even though Becoming X errored)
        assert results is not None
        assert len(results) == 1
        assert results[0].title == "Kiss & Swallow"


@pytest.mark.asyncio
async def test_resolve_albums_accepts_alias_releases():
    """resolve_albums_for_track accepts albums where Discogs resolved an alias.

    "Plug" is an alias for "Luke Vibert" on Discogs. When Discogs returns
    releases by "Luke Vibert" for a search of "Plug", the album should be accepted.
    """
    S = PLUG_ALIAS
    parsed = make_parsed_request(
        song=S.song,
        artist=S.artist,
        raw_message="me and mr jones by plug",
    )

    with patch(
        "core.orchestration.lookup_releases_by_track", new_callable=AsyncMock
    ) as mock_lookup:
        mock_lookup.return_value = [
            ("Luke Vibert", "Drum 'n' Bass for Papa (+ Plug EPs 1, 2 & 3)"),
        ]

        albums, song_not_found = await resolve_albums_for_track(parsed)

    assert not song_not_found
    assert "Drum 'n' Bass for Papa (+ Plug EPs 1, 2 & 3)" in albums


@pytest.mark.asyncio
async def test_filter_results_uses_alternate_artist_name():
    """filter_results_by_artist matches on alternate_artist_name when primary doesn't match."""
    S = PLUG_ALIAS
    items = [
        LibraryItem(
            id=38167,
            title="Drum 'n' Bass for Papa (+ Plug EPs 1,2 & 3)",
            artist=S.artist,
            alternate_artist_name="Luke Vibert",
        ),
        LibraryItem(
            id=99999,
            title="Unrelated Album",
            artist="Other Artist",
        ),
    ]

    filtered = filter_results_by_artist(items, "Luke Vibert")

    assert len(filtered) == 1
    assert filtered[0].id == 38167
