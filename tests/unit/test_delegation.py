"""Unit tests for the lookup delegation branch in handle_request."""

from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from core.dependencies import (
    get_groq_client,
    get_lookup_client,
    get_posthog_client,
    get_slack_service,
)
from models import LibraryItem, ReleaseMetadata
from routers.request import router
from services.lookup_client import LookupResponse, LookupResultItem, LookupServiceClient
from tests.conftest import make_parsed_request

# -- Fixtures -----------------------------------------------------------------


@pytest.fixture
def mock_lookup_client():
    """Create a mock LookupServiceClient."""
    return AsyncMock(spec=LookupServiceClient)


@pytest.fixture
def sample_lookup_response():
    """A typical LookupResponse with one result and artwork."""
    return LookupResponse(
        results=[
            LookupResultItem(
                library_item=LibraryItem(
                    id=42,
                    title="The Game",
                    artist="Queen",
                    call_letters="Q",
                    artist_call_number=1,
                    release_call_number=2,
                    genre="Rock",
                    format="CD",
                ),
                artwork=ReleaseMetadata(
                    album="The Game",
                    artist="Queen",
                    release_id=123,
                    artwork_url="https://img.discogs.com/test.jpg",
                    confidence=0.95,
                ),
            )
        ],
        search_type="direct",
        song_not_found=False,
        found_on_compilation=False,
        context_message=None,
        corrected_artist=None,
    )


@pytest.fixture
def app(mock_lookup_client):
    """Create a test app with the request router and delegation enabled."""
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    mock_groq = Mock()
    mock_slack = AsyncMock()
    mock_slack.webhook_url = "https://hooks.slack.com/test"

    app.dependency_overrides[get_groq_client] = lambda: mock_groq
    app.dependency_overrides[get_slack_service] = lambda: mock_slack
    app.dependency_overrides[get_posthog_client] = lambda: None
    app.dependency_overrides[get_lookup_client] = lambda: mock_lookup_client

    return app


SAMPLE_PARSED = make_parsed_request(
    song="Crazy Little Thing Called Love",
    artist="Queen",
    raw_message="play crazy little thing called love by queen",
)


# -- Tests --------------------------------------------------------------------


class TestDelegationBranch:
    """Tests for the delegation branch in handle_request."""

    @pytest.mark.asyncio
    async def test_successful_delegation(self, app, mock_lookup_client, sample_lookup_response):
        """Delegation returns results from lookup service, skipping inline pipeline."""
        mock_lookup_client.lookup.return_value = sample_lookup_response

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=SAMPLE_PARSED
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={
                        "message": "play crazy little thing called love by queen",
                        "skip_slack": True,
                    },
                )

        assert response.status_code == 200
        data = response.json()
        assert data["search_type"] == "direct"
        assert len(data["library_results"]) == 1
        assert data["library_results"][0]["id"] == 42
        assert data["library_results"][0]["artist"] == "Queen"
        assert data["artwork"]["artwork_url"] == "https://img.discogs.com/test.jpg"
        assert data["song_not_found"] is False
        assert data["found_on_compilation"] is False

    @pytest.mark.asyncio
    async def test_delegation_maps_response_fields(self, app, mock_lookup_client):
        """All LookupResponse metadata fields map to UnifiedResponse correctly."""
        mock_lookup_client.lookup.return_value = LookupResponse(
            results=[],
            search_type="compilation",
            song_not_found=True,
            found_on_compilation=True,
            context_message='Found "Abele Dance" by Manu Dibango on:',
            corrected_artist=None,
        )

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=SAMPLE_PARSED
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play queen", "skip_slack": True},
                )

        data = response.json()
        assert data["search_type"] == "compilation"
        assert data["song_not_found"] is True
        assert data["found_on_compilation"] is True
        assert data["context_message"] == 'Found "Abele Dance" by Manu Dibango on:'

    @pytest.mark.asyncio
    async def test_corrected_artist_applied(self, app, mock_lookup_client, sample_lookup_response):
        """corrected_artist from lookup replaces parsed.artist in response."""
        sample_lookup_response.corrected_artist = "Living Colour"

        parsed = make_parsed_request(artist="Living Color", raw_message="play living color")
        mock_lookup_client.lookup.return_value = sample_lookup_response

        with patch("routers.request.parse_request", new_callable=AsyncMock, return_value=parsed):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play living color", "skip_slack": True},
                )

        data = response.json()
        assert data["parsed"]["artist"] == "Living Colour"

    @pytest.mark.asyncio
    async def test_http_error_returns_502(self, app, mock_lookup_client):
        """HTTP error from lookup service returns 502."""
        mock_response = Mock()
        mock_response.status_code = 500
        mock_response.request = Mock()
        mock_lookup_client.lookup.side_effect = httpx.HTTPStatusError(
            "Internal Server Error", request=mock_response.request, response=mock_response
        )

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=SAMPLE_PARSED
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play queen", "skip_slack": True},
                )

        assert response.status_code == 502
        assert "Lookup service unavailable" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_connect_error_returns_502(self, app, mock_lookup_client):
        """Connection error to lookup service returns 502."""
        mock_lookup_client.lookup.side_effect = httpx.ConnectError("Connection refused")

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=SAMPLE_PARSED
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play queen", "skip_slack": True},
                )

        assert response.status_code == 502
        assert "Lookup service unavailable" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_timeout_returns_502(self, app, mock_lookup_client):
        """Timeout from lookup service returns 502."""
        mock_lookup_client.lookup.side_effect = httpx.TimeoutException("Read timed out")

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=SAMPLE_PARSED
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play queen", "skip_slack": True},
                )

        assert response.status_code == 502
        assert "Lookup service unavailable" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_skip_cache_forwarded(self, app, mock_lookup_client, sample_lookup_response):
        """skip_cache=True is forwarded to the lookup service."""
        mock_lookup_client.lookup.return_value = sample_lookup_response

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=SAMPLE_PARSED
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                await client.post(
                    "/api/v1/request",
                    json={
                        "message": "play queen",
                        "skip_slack": True,
                        "skip_cache": True,
                    },
                )

        call_args = mock_lookup_client.lookup.call_args
        assert call_args.kwargs.get("skip_cache") is True or (
            len(call_args.args) > 1 and call_args.args[1] is True
        )

    @pytest.mark.asyncio
    async def test_empty_results_from_delegation(self, app, mock_lookup_client):
        """Empty results from lookup service are handled correctly."""
        mock_lookup_client.lookup.return_value = LookupResponse(
            results=[],
            search_type="none",
            song_not_found=True,
            found_on_compilation=False,
        )

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=SAMPLE_PARSED
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play ZZZNONEXISTENT", "skip_slack": True},
                )

        data = response.json()
        assert data["library_results"] == []
        assert data["artwork"] is None
        assert data["search_type"] == "none"
        assert data["song_not_found"] is True

    @pytest.mark.asyncio
    async def test_multiple_results_with_artwork(self, app, mock_lookup_client):
        """Multiple results with artwork are extracted correctly."""
        mock_lookup_client.lookup.return_value = LookupResponse(
            results=[
                LookupResultItem(
                    library_item=LibraryItem(
                        id=1,
                        title="A Night at the Opera",
                        artist="Queen",
                        call_letters="Q",
                        artist_call_number=1,
                        release_call_number=1,
                        genre="Rock",
                        format="CD",
                    ),
                    artwork=ReleaseMetadata(
                        album="A Night at the Opera",
                        artist="Queen",
                        release_id=100,
                        artwork_url="https://img.discogs.com/opera.jpg",
                        confidence=0.9,
                    ),
                ),
                LookupResultItem(
                    library_item=LibraryItem(
                        id=2,
                        title="The Game",
                        artist="Queen",
                        call_letters="Q",
                        artist_call_number=1,
                        release_call_number=2,
                        genre="Rock",
                        format="CD",
                    ),
                    artwork=None,
                ),
            ],
            search_type="artist_search",
        )

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=SAMPLE_PARSED
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play queen", "skip_slack": True},
                )

        data = response.json()
        assert len(data["library_results"]) == 2
        assert data["library_results"][0]["id"] == 1
        assert data["library_results"][1]["id"] == 2
        # First artwork (non-null) is used as the top-level artwork
        assert data["artwork"]["artwork_url"] == "https://img.discogs.com/opera.jpg"

    @pytest.mark.asyncio
    async def test_lookup_request_built_from_parsed(
        self, app, mock_lookup_client, sample_lookup_response
    ):
        """LookupRequest is built from parsed fields and raw message."""
        mock_lookup_client.lookup.return_value = sample_lookup_response

        parsed = make_parsed_request(
            song="Bohemian Rhapsody",
            album="A Night at the Opera",
            artist="Queen",
            raw_message="play bohemian rhapsody by queen",
        )

        with patch("routers.request.parse_request", new_callable=AsyncMock, return_value=parsed):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                await client.post(
                    "/api/v1/request",
                    json={
                        "message": "play bohemian rhapsody by queen",
                        "skip_slack": True,
                    },
                )

        lookup_req = mock_lookup_client.lookup.call_args[0][0]
        assert lookup_req.artist == "Queen"
        assert lookup_req.song == "Bohemian Rhapsody"
        assert lookup_req.album == "A Night at the Opera"
        assert lookup_req.raw_message == "play bohemian rhapsody by queen"


class TestDelegatedCacheStats:
    """Tests for cache stats propagation in delegated mode.

    When request-o-matic delegates to library-metadata-lookup, all Discogs
    cache/API interactions happen remotely. The lookup service returns its
    cache_stats in the response. These tests verify that the delegated stats
    are surfaced in the UnifiedResponse rather than the local all-zero counters.
    """

    @pytest.mark.asyncio
    async def test_lookup_cache_stats_returned_in_response(
        self, app, mock_lookup_client, sample_lookup_response
    ):
        """Cache stats from lookup service should appear in the response."""
        remote_stats = {
            "memory_hits": 2,
            "pg_hits": 3,
            "pg_misses": 1,
            "api_calls": 4,
            "pg_time_ms": 12.5,
            "api_time_ms": 350.0,
        }
        sample_lookup_response.cache_stats = remote_stats
        mock_lookup_client.lookup.return_value = sample_lookup_response

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=SAMPLE_PARSED
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play queen", "skip_slack": True},
                )

        assert response.status_code == 200
        data = response.json()
        cache_stats = data["cache_stats"]
        assert cache_stats["memory_hits"] == 2
        assert cache_stats["pg_hits"] == 3
        assert cache_stats["pg_misses"] == 1
        assert cache_stats["api_calls"] == 4
        assert cache_stats["pg_time_ms"] == 12.5
        assert cache_stats["api_time_ms"] == 350.0

    @pytest.mark.asyncio
    async def test_lookup_cache_stats_not_all_zeros(
        self, app, mock_lookup_client, sample_lookup_response
    ):
        """Delegated response should NOT report all-zero stats when the lookup
        service reports real activity."""
        remote_stats = {
            "memory_hits": 0,
            "pg_hits": 1,
            "pg_misses": 2,
            "api_calls": 3,
            "pg_time_ms": 8.0,
            "api_time_ms": 200.0,
        }
        sample_lookup_response.cache_stats = remote_stats
        mock_lookup_client.lookup.return_value = sample_lookup_response

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=SAMPLE_PARSED
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play queen", "skip_slack": True},
                )

        data = response.json()
        cache_stats = data["cache_stats"]
        # At least one of these must be non-zero
        assert (cache_stats["pg_hits"] + cache_stats["pg_misses"] + cache_stats["api_calls"]) > 0, (
            "Delegated cache stats should not be all zeros when lookup service reports activity"
        )

    @pytest.mark.asyncio
    async def test_fallback_to_local_stats_when_lookup_returns_none(
        self, app, mock_lookup_client, sample_lookup_response
    ):
        """When lookup service returns no cache_stats, fall back to local counters."""
        sample_lookup_response.cache_stats = None
        mock_lookup_client.lookup.return_value = sample_lookup_response

        with patch(
            "routers.request.parse_request", new_callable=AsyncMock, return_value=SAMPLE_PARSED
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play queen", "skip_slack": True},
                )

        data = response.json()
        # Should still have cache_stats (from local init_cache_stats), just all zeros
        assert data["cache_stats"] is not None
        assert data["cache_stats"]["memory_hits"] == 0
