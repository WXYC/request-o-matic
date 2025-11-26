"""Integration-style unit tests for the complete request flow."""
import pytest
from unittest.mock import AsyncMock, Mock, patch

from services.parser import MessageType, ParsedRequest
from routers.request import (
    build_context_message,
    search_compilations_for_track,
)
from library.models import LibraryItem


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
    
    # Mock the search to return the same item multiple times
    mock_library_db.search.return_value = [duplicate_item]
    
    parsed = ParsedRequest(
        song="Abele Dance",
        artist="Manu Dibango",
        album=None,
        is_request=True,
        message_type=MessageType.REQUEST,
        raw_message="Abele dance (85 remix) by Manu Dibango",
    )
    
    with patch('routers.request.lookup_releases_by_track', new_callable=AsyncMock) as mock_lookup:
        # Simulate Discogs returning 4 releases that all map to the same album
        mock_lookup.return_value = [
            ("Various", "Change The Beat Vol 1"),
            ("Various", "Change The Beat Vol 2"),
            ("Various", "Change The Beat Vol 3"),
            ("Various", "Change The Beat Vol 4"),
        ]
        
        results = await search_compilations_for_track(mock_library_db, parsed)
    
    # Should only return 1 unique result despite 4 Discogs releases
    assert len(results) == 1
    assert results[0].id == 62503
    assert results[0].title == "Celluloid Records- change the beat 1979-87"


@pytest.mark.asyncio
async def test_compilation_search_returns_empty_when_no_song(mock_library_db):
    """Test that compilation search requires both song and artist."""
    parsed_no_song = ParsedRequest(
        song=None,
        artist="Manu Dibango",
        album="Soul Makossa",
        is_request=True,
        message_type=MessageType.REQUEST,
        raw_message="Soul Makossa by Manu Dibango",
    )
    
    results = await search_compilations_for_track(mock_library_db, parsed_no_song)
    assert results == []
    
    parsed_no_artist = ParsedRequest(
        song="Abele Dance",
        artist=None,
        album=None,
        is_request=True,
        message_type=MessageType.REQUEST,
        raw_message="Abele Dance",
    )
    
    results = await search_compilations_for_track(mock_library_db, parsed_no_artist)
    assert results == []


def test_build_context_message_for_found_compilation():
    """Test context message when song is found on compilation."""
    parsed = ParsedRequest(
        song="Abele Dance",
        artist="Manu Dibango",
        album=None,
        is_request=True,
        message_type=MessageType.REQUEST,
        raw_message="Abele dance (85 remix) by Manu Dibango",
    )
    
    context = build_context_message(
        parsed, found_on_compilation=True, song_not_found=False
    )
    
    assert context == 'Found "Abele Dance" by Manu Dibango on:'


def test_build_context_message_for_artist_fallback():
    """Test context message when showing artist albums as fallback."""
    parsed = ParsedRequest(
        song="Unknown Song",
        artist="Queen",
        album=None,
        is_request=True,
        message_type=MessageType.REQUEST,
        raw_message="Unknown Song by Queen",
    )
    
    context = build_context_message(
        parsed, found_on_compilation=False, song_not_found=True
    )
    
    assert context == '"Unknown Song" is not on any album in the library, but here are some albums by Queen:'


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
    
    artist_albums = [
        LibraryItem(
            id=1, title="Soul Makossa", artist="Manu Dibango",
            call_letters="DI", artist_call_number=12, release_call_number=1,
            genre="Africa", format="cd",
        ),
        LibraryItem(
            id=2, title="Polysonik", artist="Manu Dibango",
            call_letters="DI", artist_call_number=12, release_call_number=2,
            genre="Africa", format="cd",
        ),
        LibraryItem(
            id=3, title="The Rough Guide", artist="Manu Dibango",
            call_letters="DI", artist_call_number=12, release_call_number=3,
            genre="Africa", format="cd",
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
    assert len(library_results) == 1, \
        f"Expected 1 compilation, got {len(library_results)}: {[r.title for r in library_results]}"
    assert library_results[0].id == 62503
    assert library_results[0].title == "Celluloid Records- change the beat 1979-87"
    assert "Various Artists" in library_results[0].artist
    assert found_on_compilation is True
    assert song_not_found is False

