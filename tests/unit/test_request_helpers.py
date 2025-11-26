"""Unit tests for refactored request handler helper functions."""
import pytest
from unittest.mock import AsyncMock, Mock

from routers.request import (
    build_context_message,
    resolve_album_for_track,
    search_library_with_fallback,
)
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
        mock_library_db, parsed, "A Night at the Opera"
    )
    
    assert len(results) == 1
    assert results[0].artist == "Queen"
    assert fallback_used is False


@pytest.mark.asyncio
async def test_search_library_with_fallback_artist_only(mock_library_db):
    """Test library search falling back to artist only."""
    from library.models import LibraryItem
    
    # First call returns empty, second returns results
    mock_library_db.search.side_effect = [
        [],  # First search with artist+album
        [LibraryItem(
            id=2,
            artist="Queen",
            title="The Game",
            call_letters="Q",
            artist_call_number=1,
            release_call_number=2,
            genre="Rock",
            format="CD",
        )],  # Second search with artist only
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
        mock_library_db, parsed, "Unknown Album"
    )
    
    assert len(results) == 1
    assert results[0].artist == "Queen"
    assert fallback_used is True
    assert mock_library_db.search.call_count == 2

