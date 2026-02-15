"""Tests for fetch_artwork_for_items and artwork fallback logic."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from discogs.models import (
    DiscogsSearchResponse,
    DiscogsSearchResult,
    ReleaseMetadataResponse,
)
from discogs.service import DiscogsService
from routers.request import fetch_artwork_for_items


def make_library_item(*, id: int, artist: str, title: str):
    """Create a mock LibraryItem."""
    item = MagicMock()
    item.id = id
    item.artist = artist
    item.title = title
    return item


def make_search_result(
    *,
    release_id: int = 12345,
    album: str = "Test Album",
    artist: str = "Test Artist",
    artwork_url: str | None = "https://example.com/cover.jpg",
    confidence: float = 0.9,
) -> DiscogsSearchResult:
    """Create a DiscogsSearchResult for testing."""
    return DiscogsSearchResult(
        album=album,
        artist=artist,
        release_id=release_id,
        release_url=f"https://www.discogs.com/release/{release_id}",
        artwork_url=artwork_url,
        confidence=confidence,
    )


class TestFetchArtworkFallback:
    """Tests for artwork fallback to artist/label images."""

    @pytest.fixture
    def mock_discogs_service(self):
        """Create a mock DiscogsService with all needed methods."""
        service = AsyncMock(spec=DiscogsService)
        return service

    @pytest.mark.asyncio
    async def test_falls_back_to_artist_image(self, mock_discogs_service):
        """When search returns result with no artwork, fall back to artist image."""
        items = [make_library_item(id=1, artist="Autechre", title="Confield")]

        # Search returns result with no artwork
        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            results=[make_search_result(release_id=28138, artwork_url=None)]
        )
        # get_release returns release with artist_id
        mock_discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=28138,
            title="Confield",
            artist="Autechre",
            artist_id=77,
            release_url="https://www.discogs.com/release/28138",
        )
        # Artist image found
        mock_discogs_service.get_artist_image.return_value = (
            "https://i.discogs.com/artist-photo.jpg"
        )

        results = await fetch_artwork_for_items(items, mock_discogs_service)

        assert len(results) == 1
        assert results[0][1] is not None
        assert results[0][1].artwork_url == "https://i.discogs.com/artist-photo.jpg"
        mock_discogs_service.get_artist_image.assert_called_once_with(77)

    @pytest.mark.asyncio
    async def test_falls_back_to_label_image(self, mock_discogs_service):
        """When artist image also unavailable, fall back to label image."""
        items = [make_library_item(id=1, artist="Autechre", title="Confield")]

        # Search returns result with no artwork
        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            results=[make_search_result(release_id=28138, artwork_url=None)]
        )
        # get_release returns release with both IDs
        mock_discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=28138,
            title="Confield",
            artist="Autechre",
            artist_id=77,
            label_id=233,
            release_url="https://www.discogs.com/release/28138",
        )
        # Artist image not found
        mock_discogs_service.get_artist_image.return_value = None
        # Label image found
        mock_discogs_service.get_label_image.return_value = "https://i.discogs.com/label-logo.jpg"

        results = await fetch_artwork_for_items(items, mock_discogs_service)

        assert len(results) == 1
        assert results[0][1] is not None
        assert results[0][1].artwork_url == "https://i.discogs.com/label-logo.jpg"
        mock_discogs_service.get_label_image.assert_called_once_with(233)

    @pytest.mark.asyncio
    async def test_no_fallback_when_artwork_exists(self, mock_discogs_service):
        """When search returns result with artwork, no fallback calls made."""
        items = [make_library_item(id=1, artist="Autechre", title="Confield")]

        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            results=[
                make_search_result(
                    release_id=28138,
                    artwork_url="https://i.discogs.com/cover.jpg",
                )
            ]
        )

        results = await fetch_artwork_for_items(items, mock_discogs_service)

        assert len(results) == 1
        assert results[0][1] is not None
        assert results[0][1].artwork_url == "https://i.discogs.com/cover.jpg"
        # No fallback calls should have been made
        mock_discogs_service.get_release.assert_not_called()
        mock_discogs_service.get_artist_image.assert_not_called()
        mock_discogs_service.get_label_image.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_result_when_all_fallbacks_fail(self, mock_discogs_service):
        """When all fallbacks fail, result returned with artwork_url=None."""
        items = [make_library_item(id=1, artist="Autechre", title="Confield")]

        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            results=[make_search_result(release_id=28138, artwork_url=None)]
        )
        # get_release returns release but no IDs
        mock_discogs_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=28138,
            title="Confield",
            artist="Autechre",
            release_url="https://www.discogs.com/release/28138",
        )

        results = await fetch_artwork_for_items(items, mock_discogs_service)

        assert len(results) == 1
        assert results[0][1] is not None
        assert results[0][1].artwork_url is None

    @pytest.mark.asyncio
    async def test_fallback_when_get_release_returns_none(self, mock_discogs_service):
        """When get_release returns None, result still returned with no artwork."""
        items = [make_library_item(id=1, artist="Autechre", title="Confield")]

        mock_discogs_service.search.return_value = DiscogsSearchResponse(
            results=[make_search_result(release_id=28138, artwork_url=None)]
        )
        mock_discogs_service.get_release.return_value = None

        results = await fetch_artwork_for_items(items, mock_discogs_service)

        assert len(results) == 1
        assert results[0][1] is not None
        assert results[0][1].artwork_url is None
