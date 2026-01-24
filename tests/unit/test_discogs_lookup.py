"""Unit tests for discogs/lookup.py."""
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from config.settings import Settings
from discogs.lookup import (
    _get_service,
    lookup_releases_by_artist,
    lookup_releases_by_track,
)
from discogs.models import (
    DiscogsSearchRequest,
    DiscogsSearchResponse,
    DiscogsSearchResult,
    ReleaseInfo,
    TrackReleasesResponse,
)


@pytest.fixture
def mock_settings():
    """Create mock settings with Discogs token."""
    return Settings(
        groq_api_key="test_groq_key",
        discogs_token="test_discogs_token",
    )


@pytest.fixture
def mock_settings_no_token():
    """Create mock settings without Discogs token."""
    return Settings(
        groq_api_key="test_groq_key",
        discogs_token=None,
    )


class TestGetService:
    """Tests for _get_service helper function."""

    def test_returns_service_with_token(self, mock_settings):
        """Test that _get_service returns service when token is configured."""
        with patch("config.settings.get_settings", return_value=mock_settings):
            with patch("discogs.lookup.DiscogsService") as mock_service_class:
                mock_service = Mock()
                mock_service_class.return_value = mock_service

                service = _get_service()

                mock_service_class.assert_called_once_with("test_discogs_token")
                assert service is mock_service

    def test_returns_none_without_token(self, mock_settings_no_token):
        """Test that _get_service returns None when token is not configured."""
        with patch("config.settings.get_settings", return_value=mock_settings_no_token):
            service = _get_service()
            assert service is None


class TestLookupReleasesByTrack:
    """Tests for lookup_releases_by_track function."""

    @pytest.mark.asyncio
    async def test_returns_empty_without_service(self, mock_settings_no_token):
        """Test that lookup_releases_by_track returns empty list without service."""
        with patch("config.settings.get_settings", return_value=mock_settings_no_token):
            results = await lookup_releases_by_track("Song Title", "Artist Name")
            assert results == []

    @pytest.mark.asyncio
    async def test_returns_releases_from_search(self, mock_settings):
        """Test that lookup_releases_by_track returns releases from Discogs."""
        mock_service = AsyncMock()
        mock_response = TrackReleasesResponse(
            track="Song Title",
            releases=[
                ReleaseInfo(
                    artist="Artist Name",
                    album="Album One",
                    release_id=12345,
                    release_url="https://discogs.com/release/12345",
                ),
                ReleaseInfo(
                    artist="Artist Name",
                    album="Album Two",
                    release_id=67890,
                    release_url="https://discogs.com/release/67890",
                ),
            ]
        )
        mock_service.search_releases_by_track.return_value = mock_response
        # Mock validate_track_on_release to return True for all releases
        mock_service.validate_track_on_release.return_value = True

        with patch("discogs.lookup._get_service", return_value=mock_service):
            results = await lookup_releases_by_track("Song Title", "Artist Name")

        assert len(results) == 2
        assert results[0] == ("Artist Name", "Album One")
        assert results[1] == ("Artist Name", "Album Two")

    @pytest.mark.asyncio
    async def test_validates_track_on_release(self, mock_settings):
        """Test that lookup_releases_by_track validates tracks when artist provided."""
        mock_service = AsyncMock()
        mock_response = TrackReleasesResponse(
            track="Song Title",
            releases=[
                ReleaseInfo(
                    artist="Artist Name",
                    album="Valid Album",
                    release_id=12345,
                    release_url="https://discogs.com/release/12345",
                ),
                ReleaseInfo(
                    artist="Artist Name",
                    album="Invalid Album",
                    release_id=67890,
                    release_url="https://discogs.com/release/67890",
                ),
            ]
        )
        mock_service.search_releases_by_track.return_value = mock_response

        # First release is valid, second is not
        mock_service.validate_track_on_release.side_effect = [True, False]

        with patch("discogs.lookup._get_service", return_value=mock_service):
            results = await lookup_releases_by_track("Song Title", "Artist Name")

        # Only valid release should be returned
        assert len(results) == 1
        assert results[0] == ("Artist Name", "Valid Album")

    @pytest.mark.asyncio
    async def test_skips_validation_without_artist(self, mock_settings):
        """Test that lookup_releases_by_track skips validation without artist."""
        mock_service = AsyncMock()
        mock_response = TrackReleasesResponse(
            track="Song Title",
            releases=[
                ReleaseInfo(
                    artist="Unknown Artist",
                    album="Album One",
                    release_id=12345,
                    release_url="https://discogs.com/release/12345",
                ),
            ]
        )
        mock_service.search_releases_by_track.return_value = mock_response

        with patch("discogs.lookup._get_service", return_value=mock_service):
            results = await lookup_releases_by_track("Song Title")

        # Should not call validate_track_on_release when artist is None
        mock_service.validate_track_on_release.assert_not_called()
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_skips_validation_with_zero_release_id(self, mock_settings):
        """Test that lookup_releases_by_track skips validation with release_id=0."""
        mock_service = AsyncMock()
        mock_response = TrackReleasesResponse(
            track="Song Title",
            releases=[
                ReleaseInfo(
                    artist="Artist Name",
                    album="Album One",
                    release_id=0,  # Falsy release ID
                    release_url="https://discogs.com/release/0",
                ),
            ]
        )
        mock_service.search_releases_by_track.return_value = mock_response

        with patch("discogs.lookup._get_service", return_value=mock_service):
            results = await lookup_releases_by_track("Song Title", "Artist Name")

        # Should not call validate_track_on_release when release_id is falsy (0)
        mock_service.validate_track_on_release.assert_not_called()
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_respects_limit_parameter(self, mock_settings):
        """Test that lookup_releases_by_track passes limit to service."""
        mock_service = AsyncMock()
        mock_response = TrackReleasesResponse(track="Song Title", releases=[])
        mock_service.search_releases_by_track.return_value = mock_response

        with patch("discogs.lookup._get_service", return_value=mock_service):
            await lookup_releases_by_track("Song Title", "Artist Name", limit=5)

        mock_service.search_releases_by_track.assert_called_once_with(
            "Song Title", "Artist Name", 5
        )


class TestLookupReleasesByArtist:
    """Tests for lookup_releases_by_artist function."""

    @pytest.mark.asyncio
    async def test_returns_empty_without_service(self, mock_settings_no_token):
        """Test that lookup_releases_by_artist returns empty list without service."""
        with patch("config.settings.get_settings", return_value=mock_settings_no_token):
            results = await lookup_releases_by_artist("Artist Name")
            assert results == []

    @pytest.mark.asyncio
    async def test_returns_releases_from_search(self, mock_settings):
        """Test that lookup_releases_by_artist returns releases from Discogs."""
        mock_service = AsyncMock()
        mock_response = DiscogsSearchResponse(
            results=[
                DiscogsSearchResult(
                    artist="Artist Name",
                    album="Album One",
                    release_id=12345,
                    release_url="https://discogs.com/release/12345",
                ),
                DiscogsSearchResult(
                    artist="Artist Name",
                    album="Album Two",
                    release_id=67890,
                    release_url="https://discogs.com/release/67890",
                ),
            ]
        )
        mock_service.search.return_value = mock_response

        with patch("discogs.lookup._get_service", return_value=mock_service):
            results = await lookup_releases_by_artist("Artist Name")

        assert len(results) == 2
        assert results[0] == ("Artist Name", "Album One")
        assert results[1] == ("Artist Name", "Album Two")

    @pytest.mark.asyncio
    async def test_handles_none_values(self, mock_settings):
        """Test that lookup_releases_by_artist handles None artist/album."""
        mock_service = AsyncMock()
        mock_response = DiscogsSearchResponse(
            results=[
                DiscogsSearchResult(
                    artist=None,
                    album=None,
                    release_id=12345,
                    release_url="https://discogs.com/release/12345",
                ),
            ]
        )
        mock_service.search.return_value = mock_response

        with patch("discogs.lookup._get_service", return_value=mock_service):
            results = await lookup_releases_by_artist("Artist Name")

        assert len(results) == 1
        assert results[0] == ("", "")  # None values converted to empty strings

    @pytest.mark.asyncio
    async def test_respects_limit_parameter(self, mock_settings):
        """Test that lookup_releases_by_artist passes limit to service."""
        mock_service = AsyncMock()
        mock_response = DiscogsSearchResponse(results=[])
        mock_service.search.return_value = mock_response

        with patch("discogs.lookup._get_service", return_value=mock_service):
            await lookup_releases_by_artist("Artist Name", limit=5)

        mock_service.search.assert_called_once()
        call_args = mock_service.search.call_args
        assert call_args[1]["limit"] == 5

    @pytest.mark.asyncio
    async def test_creates_correct_search_request(self, mock_settings):
        """Test that lookup_releases_by_artist creates correct search request."""
        mock_service = AsyncMock()
        mock_response = DiscogsSearchResponse(results=[])
        mock_service.search.return_value = mock_response

        with patch("discogs.lookup._get_service", return_value=mock_service):
            await lookup_releases_by_artist("The Beatles")

        mock_service.search.assert_called_once()
        call_args = mock_service.search.call_args
        request = call_args[0][0]
        assert isinstance(request, DiscogsSearchRequest)
        assert request.artist == "The Beatles"
