"""Unit tests for the lookup delegation branch in handle_request."""

from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from core.dependencies import (
    get_discogs_service,
    get_groq_client,
    get_library_db,
    get_lookup_client,
    get_posthog_client,
    get_slack_service,
)
from discogs.models import DiscogsSearchResult
from library.models import LibraryItem
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
                artwork=DiscogsSearchResult(
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
    mock_db = AsyncMock()
    mock_slack = AsyncMock()
    mock_slack.webhook_url = "https://hooks.slack.com/test"

    app.dependency_overrides[get_groq_client] = lambda: mock_groq
    app.dependency_overrides[get_library_db] = lambda: mock_db
    app.dependency_overrides[get_discogs_service] = lambda: None
    app.dependency_overrides[get_slack_service] = lambda: mock_slack
    app.dependency_overrides[get_posthog_client] = lambda: None
    app.dependency_overrides[get_lookup_client] = lambda: mock_lookup_client

    return app


@pytest.fixture
def inline_app():
    """Create a test app with delegation disabled (lookup_client=None)."""
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    mock_groq = Mock()
    mock_db = AsyncMock()
    mock_db.find_similar_artist = AsyncMock(return_value=None)
    mock_slack = AsyncMock()
    mock_slack.webhook_url = "https://hooks.slack.com/test"

    app.dependency_overrides[get_groq_client] = lambda: mock_groq
    app.dependency_overrides[get_library_db] = lambda: mock_db
    app.dependency_overrides[get_discogs_service] = lambda: None
    app.dependency_overrides[get_slack_service] = lambda: mock_slack
    app.dependency_overrides[get_posthog_client] = lambda: None
    app.dependency_overrides[get_lookup_client] = lambda: None

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

        with patch("routers.request.parse_request", return_value=SAMPLE_PARSED):
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
    async def test_delegation_skips_inline_pipeline(
        self, app, mock_lookup_client, sample_lookup_response
    ):
        """When lookup_client is present, inline search functions are not called."""
        mock_lookup_client.lookup.return_value = sample_lookup_response

        with (
            patch("routers.request.parse_request", return_value=SAMPLE_PARSED),
            patch("routers.request.execute_search_pipeline") as mock_search,
            patch("routers.request.fetch_artwork_for_items") as mock_artwork,
            patch("routers.request.resolve_albums_for_track") as mock_resolve,
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                await client.post(
                    "/api/v1/request",
                    json={"message": "play queen", "skip_slack": True},
                )

        mock_search.assert_not_called()
        mock_artwork.assert_not_called()
        mock_resolve.assert_not_called()

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

        with patch("routers.request.parse_request", return_value=SAMPLE_PARSED):
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

        with patch("routers.request.parse_request", return_value=parsed):
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

        with patch("routers.request.parse_request", return_value=SAMPLE_PARSED):
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

        with patch("routers.request.parse_request", return_value=SAMPLE_PARSED):
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

        with patch("routers.request.parse_request", return_value=SAMPLE_PARSED):
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

        with patch("routers.request.parse_request", return_value=SAMPLE_PARSED):
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

        with patch("routers.request.parse_request", return_value=SAMPLE_PARSED):
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
                    artwork=DiscogsSearchResult(
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

        with patch("routers.request.parse_request", return_value=SAMPLE_PARSED):
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

        with patch("routers.request.parse_request", return_value=parsed):
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
