"""Tests for DiscogsService."""

import re
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from pytest_httpx import HTTPXMock

from discogs.memory_cache import clear_all_caches, set_skip_cache
from discogs.models import (
    DiscogsSearchRequest,
    ReleaseInfo,
    ReleaseMetadataResponse,
    TrackItem,
    TrackReleasesResponse,
)
from discogs.ratelimit import reset_rate_limiting
from discogs.service import DiscogsService

# Default test data
TEST_TRACK = "VI Scose Poise"
TEST_ALBUM = "Confield"
TEST_ARTIST = "Autechre"
TEST_RELEASE_ID = 28138

DISCOGS_API_BASE = "https://api.discogs.com"

# URL patterns for mocking (match with any query params)
SEARCH_URL_PATTERN = re.compile(r"https://api\.discogs\.com/database/search.*")
RELEASE_URL_PATTERN = re.compile(rf"https://api\.discogs\.com/releases/{TEST_RELEASE_ID}")


@pytest_asyncio.fixture
async def service():
    """Create a DiscogsService instance and clean up after."""
    svc = DiscogsService(token="test-token")
    yield svc
    await svc.close()


@pytest.fixture(autouse=True)
def clear_caches():
    """Clear all caches, rate limiting, and skip_cache state before and after each test."""
    clear_all_caches()
    reset_rate_limiting()
    set_skip_cache(False)
    yield
    clear_all_caches()
    reset_rate_limiting()
    set_skip_cache(False)


class TestSearchReleasesByTrack:
    """Tests for search_releases_by_track method."""

    @pytest.mark.asyncio
    @pytest.mark.httpx_mock(can_send_already_matched_responses=True)
    async def test_returns_releases_list(self, service: DiscogsService, httpx_mock: HTTPXMock):
        """Test returns list of releases containing track."""
        # Mock both initial search and supplementary keyword search
        httpx_mock.add_response(
            url=SEARCH_URL_PATTERN,
            json={
                "results": [
                    {
                        "id": TEST_RELEASE_ID,
                        "title": f"{TEST_ARTIST} - {TEST_ALBUM}",
                        "type": "release",
                    },
                    {
                        "id": 99999,
                        "title": "Various Artists - Electronic Compilation",
                        "type": "release",
                    },
                ]
            },
        )

        result = await service.search_releases_by_track(TEST_TRACK, TEST_ARTIST)

        assert isinstance(result, TrackReleasesResponse)
        assert result.track == TEST_TRACK
        assert result.artist == TEST_ARTIST
        assert len(result.releases) >= 1
        assert result.releases[0].album == TEST_ALBUM
        assert result.cached is False

    @pytest.mark.asyncio
    @pytest.mark.httpx_mock(can_send_already_matched_responses=True)
    async def test_identifies_compilations(self, service: DiscogsService, httpx_mock: HTTPXMock):
        """Test correctly identifies compilation albums."""
        httpx_mock.add_response(
            url=SEARCH_URL_PATTERN,
            json={
                "results": [
                    {
                        "id": 99999,
                        "title": "Various Artists - Electronic Compilation",
                        "type": "release",
                    },
                ]
            },
        )

        result = await service.search_releases_by_track(TEST_TRACK, TEST_ARTIST)

        assert len(result.releases) >= 1
        # Various Artists releases should be marked as compilations
        compilation = next((r for r in result.releases if "Various" in r.artist), None)
        if compilation:
            assert compilation.is_compilation is True

    @pytest.mark.asyncio
    @pytest.mark.httpx_mock(can_send_already_matched_responses=True)
    async def test_empty_results(self, service: DiscogsService, httpx_mock: HTTPXMock):
        """Test returns empty list when no releases found."""
        httpx_mock.add_response(
            url=SEARCH_URL_PATTERN,
            json={"results": []},
        )

        result = await service.search_releases_by_track("Nonexistent Track", TEST_ARTIST)

        assert result.releases == []
        assert result.total == 0


class TestGetRelease:
    """Tests for get_release method."""

    @pytest.mark.asyncio
    async def test_returns_full_metadata(self, service: DiscogsService, httpx_mock: HTTPXMock):
        """Test returns full release metadata."""
        httpx_mock.add_response(
            url=RELEASE_URL_PATTERN,
            json={
                "id": TEST_RELEASE_ID,
                "title": TEST_ALBUM,
                "artists": [{"name": TEST_ARTIST}],
                "year": 2001,
                "labels": [{"name": "Warp Records"}],
                "genres": ["Electronic"],
                "styles": ["IDM", "Abstract"],
                "tracklist": [
                    {"position": "1", "title": TEST_TRACK, "duration": "5:25"},
                    {"position": "2", "title": "Cfern", "duration": "6:02"},
                ],
                "images": [{"uri": "https://i.discogs.com/image.jpg"}],
            },
        )

        result = await service.get_release(TEST_RELEASE_ID)

        assert isinstance(result, ReleaseMetadataResponse)
        assert result.release_id == TEST_RELEASE_ID
        assert result.title == TEST_ALBUM
        assert result.artist == TEST_ARTIST
        assert result.year == 2001
        assert result.label == "Warp Records"
        assert "Electronic" in result.genres
        assert "IDM" in result.styles
        assert len(result.tracklist) == 2
        assert result.tracklist[0].title == TEST_TRACK
        assert result.artwork_url == "https://i.discogs.com/image.jpg"
        assert result.cached is False

    @pytest.mark.asyncio
    async def test_handles_missing_optional_fields(
        self, service: DiscogsService, httpx_mock: HTTPXMock
    ):
        """Test handles releases with missing optional fields."""
        httpx_mock.add_response(
            url=RELEASE_URL_PATTERN,
            json={
                "id": TEST_RELEASE_ID,
                "title": TEST_ALBUM,
                "artists": [{"name": TEST_ARTIST}],
                "tracklist": [],
            },
        )

        result = await service.get_release(TEST_RELEASE_ID)

        assert result is not None
        assert result.release_id == TEST_RELEASE_ID
        assert result.year is None
        assert result.label is None
        assert result.genres == []
        assert result.artwork_url is None

    @pytest.mark.asyncio
    async def test_caches_release(self, service: DiscogsService, httpx_mock: HTTPXMock):
        """Test release metadata is cached."""
        httpx_mock.add_response(
            url=RELEASE_URL_PATTERN,
            json={
                "id": TEST_RELEASE_ID,
                "title": TEST_ALBUM,
                "artists": [{"name": TEST_ARTIST}],
                "tracklist": [],
            },
        )

        result1 = await service.get_release(TEST_RELEASE_ID)
        assert result1 is not None
        assert result1.cached is False

        result2 = await service.get_release(TEST_RELEASE_ID)
        assert result2 is not None
        assert result2.cached is True

        # Only one HTTP request should have been made
        assert len(httpx_mock.get_requests()) == 1

    @pytest.mark.asyncio
    @pytest.mark.httpx_mock(can_send_already_matched_responses=True)
    async def test_handles_rate_limit(self, service: DiscogsService, httpx_mock: HTTPXMock):
        """Test graceful handling of rate limit."""
        # With retries, we need multiple 429 responses
        httpx_mock.add_response(
            url=RELEASE_URL_PATTERN,
            status_code=429,
        )

        result = await service.get_release(TEST_RELEASE_ID)

        # Should return None after exhausting retries
        assert result is None


class TestSearch:
    """Tests for general search method."""

    @pytest.mark.asyncio
    async def test_search_by_artist_and_album(self, service: DiscogsService, httpx_mock: HTTPXMock):
        """Test search by artist and album."""
        httpx_mock.add_response(
            url=SEARCH_URL_PATTERN,
            json={
                "results": [
                    {
                        "id": TEST_RELEASE_ID,
                        "title": f"{TEST_ARTIST} - {TEST_ALBUM}",
                        "type": "release",
                        "thumb": "https://i.discogs.com/thumb.jpg",
                    }
                ]
            },
        )

        request = DiscogsSearchRequest(artist=TEST_ARTIST, album=TEST_ALBUM)
        result = await service.search(request)

        assert result.total >= 1
        assert result.results[0].album == TEST_ALBUM
        assert result.results[0].artist == TEST_ARTIST
        assert result.results[0].artwork_url == "https://i.discogs.com/thumb.jpg"

    @pytest.mark.asyncio
    async def test_search_calculates_confidence(
        self, service: DiscogsService, httpx_mock: HTTPXMock
    ):
        """Test search results have confidence scores."""
        httpx_mock.add_response(
            url=SEARCH_URL_PATTERN,
            json={
                "results": [
                    {
                        "id": TEST_RELEASE_ID,
                        "title": f"{TEST_ARTIST} - {TEST_ALBUM}",
                        "type": "release",
                        "thumb": "https://i.discogs.com/thumb.jpg",
                    }
                ]
            },
        )

        request = DiscogsSearchRequest(artist=TEST_ARTIST, album=TEST_ALBUM)
        result = await service.search(request)

        # Exact match should have high confidence
        assert result.results[0].confidence > 0.5

    @pytest.mark.asyncio
    @pytest.mark.httpx_mock(can_send_already_matched_responses=True)
    async def test_search_empty_results(self, service: DiscogsService, httpx_mock: HTTPXMock):
        """Test search with no results."""
        # Mock both strict search and fuzzy fallback search
        httpx_mock.add_response(
            url=SEARCH_URL_PATTERN,
            json={"results": []},
        )

        request = DiscogsSearchRequest(artist="Nonexistent Artist", album="Nonexistent Album")
        result = await service.search(request)

        assert result.results == []
        assert result.total == 0


class TestValidateTrackOnRelease:
    """Tests for validate_track_on_release method."""

    @pytest.mark.asyncio
    async def test_validates_track_exists(self, service: DiscogsService, httpx_mock: HTTPXMock):
        """Test validates track exists on release."""
        httpx_mock.add_response(
            url=RELEASE_URL_PATTERN,
            json={
                "id": TEST_RELEASE_ID,
                "title": TEST_ALBUM,
                "artists": [{"name": TEST_ARTIST}],
                "tracklist": [
                    {"position": "1", "title": TEST_TRACK, "artists": [{"name": TEST_ARTIST}]},
                ],
            },
        )

        result = await service.validate_track_on_release(TEST_RELEASE_ID, TEST_TRACK, TEST_ARTIST)

        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_when_track_not_found(
        self, service: DiscogsService, httpx_mock: HTTPXMock
    ):
        """Test returns False when track not on release."""
        httpx_mock.add_response(
            url=RELEASE_URL_PATTERN,
            json={
                "id": TEST_RELEASE_ID,
                "title": TEST_ALBUM,
                "artists": [{"name": TEST_ARTIST}],
                "tracklist": [
                    {
                        "position": "1",
                        "title": "Different Track",
                        "artists": [{"name": TEST_ARTIST}],
                    },
                ],
            },
        )

        result = await service.validate_track_on_release(TEST_RELEASE_ID, TEST_TRACK, TEST_ARTIST)

        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_artist_not_found(
        self, service: DiscogsService, httpx_mock: HTTPXMock
    ):
        """Test returns False when artist not on track."""
        httpx_mock.add_response(
            url=RELEASE_URL_PATTERN,
            json={
                "id": TEST_RELEASE_ID,
                "title": TEST_ALBUM,
                "artists": [{"name": "Different Artist"}],
                "tracklist": [
                    {
                        "position": "1",
                        "title": TEST_TRACK,
                        "artists": [{"name": "Different Artist"}],
                    },
                ],
            },
        )

        result = await service.validate_track_on_release(TEST_RELEASE_ID, TEST_TRACK, TEST_ARTIST)

        assert result is False


class TestRequestWithRetry:
    """Tests for _request_with_retry rate limiting and retry behavior."""

    @pytest.mark.asyncio
    async def test_successful_request(self, service: DiscogsService, httpx_mock: HTTPXMock):
        """Test successful request returns response."""
        httpx_mock.add_response(
            url=SEARCH_URL_PATTERN,
            json={"results": []},
        )

        response = await service._request_with_retry("GET", "/database/search", params={})

        assert response is not None
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_retries_on_429(self, service: DiscogsService, httpx_mock: HTTPXMock):
        """Test retries request on 429 status code."""
        # First request returns 429, second succeeds
        httpx_mock.add_response(
            url=SEARCH_URL_PATTERN,
            status_code=429,
        )
        httpx_mock.add_response(
            url=SEARCH_URL_PATTERN,
            json={"results": []},
        )

        response = await service._request_with_retry(
            "GET", "/database/search", params={}, max_retries=1
        )

        assert response is not None
        assert response.status_code == 200
        assert len(httpx_mock.get_requests()) == 2

    @pytest.mark.asyncio
    async def test_returns_none_after_max_retries(
        self, service: DiscogsService, httpx_mock: HTTPXMock
    ):
        """Test returns None after exhausting retries."""
        # All requests return 429
        httpx_mock.add_response(url=SEARCH_URL_PATTERN, status_code=429)
        httpx_mock.add_response(url=SEARCH_URL_PATTERN, status_code=429)

        response = await service._request_with_retry(
            "GET", "/database/search", params={}, max_retries=1
        )

        assert response is None
        # Initial request + 1 retry = 2 requests
        assert len(httpx_mock.get_requests()) == 2

    @pytest.mark.asyncio
    async def test_logs_rate_limit_remaining(
        self, service: DiscogsService, httpx_mock: HTTPXMock, caplog
    ):
        """Test logs X-Discogs-Ratelimit-Remaining header."""
        import logging

        caplog.set_level(logging.DEBUG)

        httpx_mock.add_response(
            url=SEARCH_URL_PATTERN,
            json={"results": []},
            headers={"X-Discogs-Ratelimit-Remaining": "42"},
        )

        await service._request_with_retry("GET", "/database/search", params={})

        assert "rate limit remaining: 42" in caplog.text.lower()

    @pytest.mark.asyncio
    async def test_handles_request_error(self, service: DiscogsService, httpx_mock: HTTPXMock):
        """Test handles httpx.RequestError gracefully."""
        import httpx

        httpx_mock.add_exception(httpx.RequestError("Connection failed"))

        response = await service._request_with_retry("GET", "/database/search", params={})

        assert response is None


class TestCacheIntegration:
    """Tests for PostgreSQL cache integration in DiscogsService."""

    @pytest.fixture
    def mock_cache_service(self):
        """Create a mock cache service."""
        from discogs.cache_service import DiscogsCacheService

        cache = MagicMock(spec=DiscogsCacheService)
        cache.search_releases_by_track = AsyncMock()
        cache.search_releases = AsyncMock()
        cache.get_release = AsyncMock()
        cache.write_release = AsyncMock()
        cache.validate_track_on_release = AsyncMock()
        cache.is_available = AsyncMock(return_value=True)
        return cache

    @pytest_asyncio.fixture
    async def service_with_cache(self, mock_cache_service):
        """Create a DiscogsService with cache service."""
        svc = DiscogsService(token="test-token", cache_service=mock_cache_service)
        yield svc
        await svc.close()

    @pytest.mark.asyncio
    async def test_search_releases_cache_hit_skips_api(
        self, service_with_cache: DiscogsService, mock_cache_service, httpx_mock: HTTPXMock
    ):
        """Test cache hit skips API call."""
        # Cache returns results
        mock_cache_service.search_releases_by_track.return_value = [
            ReleaseInfo(
                album=TEST_ALBUM,
                artist=TEST_ARTIST,
                release_id=TEST_RELEASE_ID,
                release_url=f"https://www.discogs.com/release/{TEST_RELEASE_ID}",
                is_compilation=False,
            )
        ]

        result = await service_with_cache.search_releases_by_track(TEST_TRACK, TEST_ARTIST)

        assert len(result.releases) == 1
        assert result.releases[0].album == TEST_ALBUM
        # No HTTP requests should have been made
        assert len(httpx_mock.get_requests()) == 0

    @pytest.mark.asyncio
    @pytest.mark.httpx_mock(can_send_already_matched_responses=True)
    async def test_search_releases_cache_miss_calls_api(
        self, service_with_cache: DiscogsService, mock_cache_service, httpx_mock: HTTPXMock
    ):
        """Test cache miss falls back to API."""
        # Cache returns empty (miss)
        mock_cache_service.search_releases_by_track.return_value = []

        # API returns results
        httpx_mock.add_response(
            url=SEARCH_URL_PATTERN,
            json={
                "results": [
                    {
                        "id": TEST_RELEASE_ID,
                        "title": f"{TEST_ARTIST} - {TEST_ALBUM}",
                        "type": "release",
                    }
                ]
            },
        )

        result = await service_with_cache.search_releases_by_track(TEST_TRACK, TEST_ARTIST)

        assert len(result.releases) >= 1
        # HTTP request should have been made
        assert len(httpx_mock.get_requests()) >= 1

    @pytest.mark.asyncio
    async def test_get_release_cache_hit_skips_api(
        self, service_with_cache: DiscogsService, mock_cache_service, httpx_mock: HTTPXMock
    ):
        """Test get_release cache hit skips API call."""
        # Cache returns release
        mock_cache_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=TEST_RELEASE_ID,
            title=TEST_ALBUM,
            artist=TEST_ARTIST,
            year=2001,
            tracklist=[TrackItem(position="1", title=TEST_TRACK)],
            release_url=f"https://www.discogs.com/release/{TEST_RELEASE_ID}",
            cached=True,
        )

        result = await service_with_cache.get_release(TEST_RELEASE_ID)

        assert result is not None
        assert result.title == TEST_ALBUM
        assert result.cached is True
        # No HTTP requests should have been made
        assert len(httpx_mock.get_requests()) == 0

    @pytest.mark.asyncio
    async def test_get_release_cache_miss_calls_api_and_writes_back(
        self, service_with_cache: DiscogsService, mock_cache_service, httpx_mock: HTTPXMock
    ):
        """Test cache miss calls API and writes result back to cache."""
        # Cache returns None (miss)
        mock_cache_service.get_release.return_value = None

        # API returns release
        httpx_mock.add_response(
            url=RELEASE_URL_PATTERN,
            json={
                "id": TEST_RELEASE_ID,
                "title": TEST_ALBUM,
                "artists": [{"name": TEST_ARTIST}],
                "year": 2001,
                "tracklist": [{"position": "1", "title": TEST_TRACK}],
            },
        )

        result = await service_with_cache.get_release(TEST_RELEASE_ID)

        assert result is not None
        assert result.title == TEST_ALBUM
        # HTTP request should have been made
        assert len(httpx_mock.get_requests()) == 1
        # Result should be written back to cache
        mock_cache_service.write_release.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.httpx_mock(can_send_already_matched_responses=True)
    async def test_cache_unavailable_continues_with_api(
        self, service_with_cache: DiscogsService, mock_cache_service, httpx_mock: HTTPXMock
    ):
        """Test service continues with API when cache is unavailable."""
        from discogs.cache_service import CacheUnavailableError

        # Cache raises error
        mock_cache_service.get_release.side_effect = CacheUnavailableError("Connection failed")
        mock_cache_service.write_release.side_effect = CacheUnavailableError("Connection failed")

        # API returns release
        httpx_mock.add_response(
            url=RELEASE_URL_PATTERN,
            json={
                "id": TEST_RELEASE_ID,
                "title": TEST_ALBUM,
                "artists": [{"name": TEST_ARTIST}],
                "tracklist": [],
            },
        )

        result = await service_with_cache.get_release(TEST_RELEASE_ID)

        assert result is not None
        assert result.title == TEST_ALBUM
        # Should have fallen back to API
        assert len(httpx_mock.get_requests()) == 1

    @pytest.mark.asyncio
    async def test_cache_unavailable_logged(
        self, service_with_cache: DiscogsService, mock_cache_service, httpx_mock: HTTPXMock, caplog
    ):
        """Test cache unavailability is logged."""
        import logging

        from discogs.cache_service import CacheUnavailableError

        caplog.set_level(logging.WARNING)

        # Cache raises error
        mock_cache_service.get_release.side_effect = CacheUnavailableError("Connection failed")
        mock_cache_service.write_release.side_effect = CacheUnavailableError("Connection failed")

        # API returns release
        httpx_mock.add_response(
            url=RELEASE_URL_PATTERN,
            json={
                "id": TEST_RELEASE_ID,
                "title": TEST_ALBUM,
                "artists": [{"name": TEST_ARTIST}],
                "tracklist": [],
            },
        )

        await service_with_cache.get_release(TEST_RELEASE_ID)

        assert "cache" in caplog.text.lower()

    @pytest.mark.asyncio
    async def test_validate_track_cache_hit_skips_api(
        self, service_with_cache: DiscogsService, mock_cache_service, httpx_mock: HTTPXMock
    ):
        """Test validate_track_on_release uses cache when available."""
        # Cache returns True (track found)
        mock_cache_service.validate_track_on_release.return_value = True

        result = await service_with_cache.validate_track_on_release(
            TEST_RELEASE_ID, TEST_TRACK, TEST_ARTIST
        )

        assert result is True
        # No HTTP requests should have been made
        assert len(httpx_mock.get_requests()) == 0

    @pytest.mark.asyncio
    async def test_validate_track_cache_miss_calls_api(
        self, service_with_cache: DiscogsService, mock_cache_service, httpx_mock: HTTPXMock
    ):
        """Test validate_track_on_release falls back to API on cache miss."""
        # Cache validation returns None (not cached)
        mock_cache_service.validate_track_on_release.return_value = None
        # Cache get_release also returns None (forcing API call)
        mock_cache_service.get_release.return_value = None

        # API returns release with track
        httpx_mock.add_response(
            url=RELEASE_URL_PATTERN,
            json={
                "id": TEST_RELEASE_ID,
                "title": TEST_ALBUM,
                "artists": [{"name": TEST_ARTIST}],
                "tracklist": [
                    {"position": "1", "title": TEST_TRACK, "artists": [{"name": TEST_ARTIST}]}
                ],
            },
        )

        result = await service_with_cache.validate_track_on_release(
            TEST_RELEASE_ID, TEST_TRACK, TEST_ARTIST
        )

        assert result is True
        # HTTP request should have been made
        assert len(httpx_mock.get_requests()) == 1

    @pytest.mark.asyncio
    async def test_search_cache_hit_skips_api(
        self, service_with_cache: DiscogsService, mock_cache_service, httpx_mock: HTTPXMock
    ):
        """Test search() cache hit returns cached results without API call."""
        mock_cache_service.search_releases.return_value = [
            {
                "release_id": TEST_RELEASE_ID,
                "title": TEST_ALBUM,
                "artist_name": TEST_ARTIST,
                "artwork_url": "https://img.discogs.com/thumb.jpg",
            }
        ]

        request = DiscogsSearchRequest(artist=TEST_ARTIST, album=TEST_ALBUM)
        result = await service_with_cache.search(request)

        assert result.total >= 1
        assert result.results[0].release_id == TEST_RELEASE_ID
        assert result.results[0].album == TEST_ALBUM
        assert result.results[0].artist == TEST_ARTIST
        assert result.results[0].artwork_url == "https://img.discogs.com/thumb.jpg"
        assert result.cached is True
        # No HTTP requests should have been made
        assert len(httpx_mock.get_requests()) == 0

    @pytest.mark.asyncio
    @pytest.mark.httpx_mock(can_send_already_matched_responses=True)
    async def test_search_cache_miss_falls_back_to_api(
        self, service_with_cache: DiscogsService, mock_cache_service, httpx_mock: HTTPXMock
    ):
        """Test search() cache miss falls back to API."""
        mock_cache_service.search_releases.return_value = []

        httpx_mock.add_response(
            url=SEARCH_URL_PATTERN,
            json={
                "results": [
                    {
                        "id": TEST_RELEASE_ID,
                        "title": f"{TEST_ARTIST} - {TEST_ALBUM}",
                        "type": "release",
                        "thumb": "https://i.discogs.com/thumb.jpg",
                    }
                ]
            },
        )

        request = DiscogsSearchRequest(artist=TEST_ARTIST, album=TEST_ALBUM)
        result = await service_with_cache.search(request)

        assert result.total >= 1
        assert result.results[0].album == TEST_ALBUM
        assert result.cached is False
        # HTTP request should have been made
        assert len(httpx_mock.get_requests()) >= 1

    @pytest.mark.asyncio
    @pytest.mark.httpx_mock(can_send_already_matched_responses=True)
    async def test_search_cache_error_falls_back_to_api(
        self, service_with_cache: DiscogsService, mock_cache_service, httpx_mock: HTTPXMock
    ):
        """Test search() falls back to API when cache raises an exception."""
        from discogs.cache_service import CacheUnavailableError

        mock_cache_service.search_releases.side_effect = CacheUnavailableError("Connection failed")

        httpx_mock.add_response(
            url=SEARCH_URL_PATTERN,
            json={
                "results": [
                    {
                        "id": TEST_RELEASE_ID,
                        "title": f"{TEST_ARTIST} - {TEST_ALBUM}",
                        "type": "release",
                        "thumb": "https://i.discogs.com/thumb.jpg",
                    }
                ]
            },
        )

        request = DiscogsSearchRequest(artist=TEST_ARTIST, album=TEST_ALBUM)
        result = await service_with_cache.search(request)

        assert result.total >= 1
        assert result.cached is False
        assert len(httpx_mock.get_requests()) >= 1

    @pytest.mark.asyncio
    async def test_search_no_cache_service_goes_to_api(
        self, service: DiscogsService, httpx_mock: HTTPXMock
    ):
        """Test search() without cache_service goes straight to API."""
        httpx_mock.add_response(
            url=SEARCH_URL_PATTERN,
            json={
                "results": [
                    {
                        "id": TEST_RELEASE_ID,
                        "title": f"{TEST_ARTIST} - {TEST_ALBUM}",
                        "type": "release",
                        "thumb": "https://i.discogs.com/thumb.jpg",
                    }
                ]
            },
        )

        request = DiscogsSearchRequest(artist=TEST_ARTIST, album=TEST_ALBUM)
        result = await service.search(request)

        assert result.total >= 1
        assert result.cached is False
        assert len(httpx_mock.get_requests()) == 1


class TestSkipCacheBypassesPgCache:
    """Tests that the skip_cache ContextVar bypasses PG cache lookups."""

    @pytest.fixture
    def mock_cache_service(self):
        """Create a mock cache service."""
        from discogs.cache_service import DiscogsCacheService

        cache = MagicMock(spec=DiscogsCacheService)
        cache.search_releases_by_track = AsyncMock()
        cache.search_releases = AsyncMock()
        cache.get_release = AsyncMock()
        cache.write_release = AsyncMock()
        cache.validate_track_on_release = AsyncMock()
        cache.is_available = AsyncMock(return_value=True)
        return cache

    @pytest_asyncio.fixture
    async def service_with_cache(self, mock_cache_service):
        """Create a DiscogsService with cache service."""
        svc = DiscogsService(token="test-token", cache_service=mock_cache_service)
        yield svc
        await svc.close()

    @pytest.mark.asyncio
    @pytest.mark.httpx_mock(can_send_already_matched_responses=True)
    async def test_search_skips_pg_cache_when_flag_set(
        self, service_with_cache: DiscogsService, mock_cache_service, httpx_mock: HTTPXMock
    ):
        """Test search() skips PG cache when skip_cache flag is set."""
        httpx_mock.add_response(
            url=SEARCH_URL_PATTERN,
            json={
                "results": [
                    {
                        "id": TEST_RELEASE_ID,
                        "title": f"{TEST_ARTIST} - {TEST_ALBUM}",
                        "type": "release",
                        "thumb": "https://i.discogs.com/thumb.jpg",
                    }
                ]
            },
        )

        set_skip_cache(True)
        request = DiscogsSearchRequest(artist=TEST_ARTIST, album=TEST_ALBUM)
        result = await service_with_cache.search(request)

        # PG cache should NOT have been consulted
        mock_cache_service.search_releases.assert_not_called()
        # API should have been called
        assert len(httpx_mock.get_requests()) >= 1
        assert result.total >= 1

    @pytest.mark.asyncio
    async def test_get_release_skips_pg_cache_when_flag_set(
        self, service_with_cache: DiscogsService, mock_cache_service, httpx_mock: HTTPXMock
    ):
        """Test get_release() skips PG cache when skip_cache flag is set."""
        httpx_mock.add_response(
            url=RELEASE_URL_PATTERN,
            json={
                "id": TEST_RELEASE_ID,
                "title": TEST_ALBUM,
                "artists": [{"name": TEST_ARTIST}],
                "tracklist": [],
            },
        )

        set_skip_cache(True)
        result = await service_with_cache.get_release(TEST_RELEASE_ID)

        # PG cache should NOT have been consulted
        mock_cache_service.get_release.assert_not_called()
        # Write-back should also be skipped
        mock_cache_service.write_release.assert_not_called()
        # API should have been called
        assert len(httpx_mock.get_requests()) == 1
        assert result is not None

    @pytest.mark.asyncio
    @pytest.mark.httpx_mock(can_send_already_matched_responses=True)
    async def test_search_releases_by_track_skips_pg_cache_when_flag_set(
        self, service_with_cache: DiscogsService, mock_cache_service, httpx_mock: HTTPXMock
    ):
        """Test search_releases_by_track() skips PG cache when skip_cache flag is set."""
        httpx_mock.add_response(
            url=SEARCH_URL_PATTERN,
            json={
                "results": [
                    {
                        "id": TEST_RELEASE_ID,
                        "title": f"{TEST_ARTIST} - {TEST_ALBUM}",
                        "type": "release",
                    }
                ]
            },
        )

        set_skip_cache(True)
        await service_with_cache.search_releases_by_track(TEST_TRACK, TEST_ARTIST)

        # PG cache should NOT have been consulted
        mock_cache_service.search_releases_by_track.assert_not_called()
        # API should have been called
        assert len(httpx_mock.get_requests()) >= 1

    @pytest.mark.asyncio
    async def test_validate_track_skips_pg_cache_when_flag_set(
        self, service_with_cache: DiscogsService, mock_cache_service, httpx_mock: HTTPXMock
    ):
        """Test validate_track_on_release() skips PG cache when skip_cache flag is set."""
        httpx_mock.add_response(
            url=RELEASE_URL_PATTERN,
            json={
                "id": TEST_RELEASE_ID,
                "title": TEST_ALBUM,
                "artists": [{"name": TEST_ARTIST}],
                "tracklist": [
                    {"position": "1", "title": TEST_TRACK, "artists": [{"name": TEST_ARTIST}]}
                ],
            },
        )

        set_skip_cache(True)
        result = await service_with_cache.validate_track_on_release(
            TEST_RELEASE_ID, TEST_TRACK, TEST_ARTIST
        )

        # PG cache should NOT have been consulted
        mock_cache_service.validate_track_on_release.assert_not_called()
        # Should have fallen through to API
        assert result is True
