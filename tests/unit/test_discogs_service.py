"""Tests for DiscogsService."""

import re

import pytest
import pytest_asyncio
from pytest_httpx import HTTPXMock

from discogs.cache import clear_all_caches
from discogs.models import (
    DiscogsSearchRequest,
    ReleaseMetadataResponse,
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
    """Clear all caches and rate limiting state before and after each test."""
    clear_all_caches()
    reset_rate_limiting()
    yield
    clear_all_caches()
    reset_rate_limiting()


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
