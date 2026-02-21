"""Tests for Discogs router endpoints."""

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from core.dependencies import get_discogs_service
from discogs.models import (
    DiscogsSearchResponse,
    DiscogsSearchResult,
    ReleaseInfo,
    ReleaseMetadataResponse,
    TrackItem,
    TrackReleasesResponse,
)
from discogs.router import router
from discogs.service import DiscogsService

# Default test data
TEST_TRACK = "VI Scose Poise"
TEST_ALBUM = "Confield"
TEST_ARTIST = "Autechre"
TEST_RELEASE_ID = 28138


@pytest.fixture
def mock_service():
    """Create a mock DiscogsService."""
    return AsyncMock(spec=DiscogsService)


@pytest.fixture
def app(mock_service: AsyncMock):
    """Create a test FastAPI app with the discogs router and mocked service."""
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    # Override the dependency with the mock service
    app.dependency_overrides[get_discogs_service] = lambda: mock_service

    return app


@pytest_asyncio.fixture
async def async_client(app: FastAPI):
    """Create async test client."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


class TestTrackReleasesEndpoint:
    """Tests for GET /api/v1/discogs/track-releases endpoint."""

    @pytest.mark.asyncio
    async def test_returns_releases_list(self, async_client: AsyncClient, mock_service: AsyncMock):
        """Test successful releases lookup."""
        mock_service.search_releases_by_track.return_value = TrackReleasesResponse(
            track=TEST_TRACK,
            artist=TEST_ARTIST,
            releases=[
                ReleaseInfo(
                    album=TEST_ALBUM,
                    artist=TEST_ARTIST,
                    release_id=TEST_RELEASE_ID,
                    is_compilation=False,
                )
            ],
            total=1,
            cached=False,
        )

        response = await async_client.get(
            "/api/v1/discogs/track-releases",
            params={"track": TEST_TRACK, "artist": TEST_ARTIST},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["track"] == TEST_TRACK
        assert len(data["releases"]) == 1
        assert data["releases"][0]["album"] == TEST_ALBUM

    @pytest.mark.asyncio
    async def test_requires_track_param(self, async_client: AsyncClient):
        """Test 422 when track parameter is missing."""
        response = await async_client.get("/api/v1/discogs/track-releases")

        assert response.status_code == 422


class TestReleaseEndpoint:
    """Tests for GET /api/v1/discogs/release/{id} endpoint."""

    @pytest.mark.asyncio
    async def test_returns_full_metadata(self, async_client: AsyncClient, mock_service: AsyncMock):
        """Test successful release metadata lookup."""
        mock_service.get_release.return_value = ReleaseMetadataResponse(
            release_id=TEST_RELEASE_ID,
            title=TEST_ALBUM,
            artist=TEST_ARTIST,
            year=2001,
            label="Warp Records",
            genres=["Electronic"],
            styles=["IDM", "Abstract"],
            tracklist=[
                TrackItem(position="1", title=TEST_TRACK, duration="5:25"),
            ],
            artwork_url="https://i.discogs.com/image.jpg",
            cached=False,
        )

        response = await async_client.get(f"/api/v1/discogs/release/{TEST_RELEASE_ID}")

        assert response.status_code == 200
        data = response.json()
        assert data["release_id"] == TEST_RELEASE_ID
        assert data["title"] == TEST_ALBUM
        assert data["year"] == 2001
        assert "Electronic" in data["genres"]

    @pytest.mark.asyncio
    async def test_returns_404_when_not_found(
        self, async_client: AsyncClient, mock_service: AsyncMock
    ):
        """Test 404 when release not found."""
        mock_service.get_release.return_value = None

        response = await async_client.get("/api/v1/discogs/release/99999999")

        assert response.status_code == 404


class TestSearchEndpoint:
    """Tests for POST /api/v1/discogs/search endpoint."""

    @pytest.mark.asyncio
    async def test_search_by_artist_and_album(
        self, async_client: AsyncClient, mock_service: AsyncMock
    ):
        """Test search with artist and album."""
        mock_service.search.return_value = DiscogsSearchResponse(
            results=[
                DiscogsSearchResult(
                    album=TEST_ALBUM,
                    artist=TEST_ARTIST,
                    release_id=TEST_RELEASE_ID,
                    artwork_url="https://i.discogs.com/thumb.jpg",
                    confidence=0.9,
                )
            ],
            total=1,
            cached=False,
        )

        response = await async_client.post(
            "/api/v1/discogs/search",
            json={"artist": TEST_ARTIST, "album": TEST_ALBUM},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 1
        assert data["results"][0]["album"] == TEST_ALBUM

    @pytest.mark.asyncio
    async def test_requires_at_least_one_param(
        self, async_client: AsyncClient, mock_service: AsyncMock
    ):
        """Test 400 when no search parameters provided."""
        response = await async_client.post(
            "/api/v1/discogs/search",
            json={},
        )

        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_accepts_limit_parameter(
        self, async_client: AsyncClient, mock_service: AsyncMock
    ):
        """Test search accepts limit query parameter."""
        mock_service.search.return_value = DiscogsSearchResponse(
            results=[],
            total=0,
            cached=False,
        )

        response = await async_client.post(
            "/api/v1/discogs/search",
            params={"limit": 10},
            json={"artist": TEST_ARTIST},
        )

        assert response.status_code == 200
        # Verify service was called with the limit
        mock_service.search.assert_called_once()
        call_args = mock_service.search.call_args
        assert call_args.kwargs.get("limit") == 10


class TestServiceUnavailable:
    """Tests for service unavailable scenarios."""

    @pytest.mark.asyncio
    async def test_returns_503_when_service_unavailable(self):
        """Test 503 when Discogs service is not configured."""
        # Create app with None service to simulate unavailable service
        app = FastAPI()
        app.include_router(router, prefix="/api/v1")
        app.dependency_overrides[get_discogs_service] = lambda: None

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(
                "/api/v1/discogs/track-releases",
                params={"track": TEST_TRACK},
            )

        assert response.status_code == 503
        assert "not configured" in response.json()["detail"].lower()
