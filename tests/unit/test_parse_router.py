"""Unit tests for routers/parse.py."""

from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routers.parse import router
from services.parser import MessageType
from tests.conftest import make_parsed_request


@pytest.fixture
def mock_groq_client():
    """Create a mock Groq client."""
    return Mock()


@pytest.fixture
def app(mock_groq_client):
    """Create a test app with the parse router."""
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    # Override the dependency
    from core.dependencies import get_groq_client

    app.dependency_overrides[get_groq_client] = lambda: mock_groq_client

    return app


@pytest.fixture
def sample_parsed_request():
    """Create a sample parsed request."""
    return make_parsed_request(
        song="Bohemian Rhapsody",
        album="A Night at the Opera",
        artist="Queen",
        raw_message="Play Bohemian Rhapsody by Queen",
    )


class TestParseEndpoint:
    """Tests for the /parse endpoint."""

    @pytest.mark.asyncio
    async def test_parse_successful(self, app, mock_groq_client, sample_parsed_request):
        """Test successful message parsing."""
        with patch("routers.parse.parse_request", new_callable=AsyncMock) as mock_parse:
            mock_parse.return_value = sample_parsed_request

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/parse", json={"message": "Play Bohemian Rhapsody by Queen"}
                )

            assert response.status_code == 200
            data = response.json()

            assert data["song"] == "Bohemian Rhapsody"
            assert data["album"] == "A Night at the Opera"
            assert data["artist"] == "Queen"
            assert data["is_request"] is True
            assert data["message_type"] == "request"

            mock_parse.assert_called_once_with("Play Bohemian Rhapsody by Queen", mock_groq_client)

    @pytest.mark.asyncio
    async def test_parse_empty_message_returns_400(self, app, mock_groq_client):
        """Test that empty message returns 400."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/v1/parse", json={"message": ""})

        assert response.status_code == 400
        assert "Message cannot be empty" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_parse_whitespace_only_message_returns_400(self, app, mock_groq_client):
        """Test that whitespace-only message returns 400."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/v1/parse", json={"message": "   \t\n  "})

        assert response.status_code == 400
        assert "Message cannot be empty" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_parse_rate_limit_returns_429(self, app, mock_groq_client):
        """Test that Groq rate limit returns 429."""
        from groq import RateLimitError

        resp = httpx.Response(429, request=httpx.Request("POST", "https://api.groq.com/test"))
        with patch("routers.parse.parse_request", new_callable=AsyncMock) as mock_parse:
            mock_parse.side_effect = RateLimitError(
                message="Rate limit exceeded", response=resp, body=None
            )

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post("/api/v1/parse", json={"message": "some message"})

            assert response.status_code == 429
            assert "Rate limit exceeded" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_parse_value_error_returns_500(self, app, mock_groq_client):
        """Test that ValueError from parser returns 500."""
        with patch("routers.parse.parse_request", new_callable=AsyncMock) as mock_parse:
            mock_parse.side_effect = ValueError("Invalid message format")

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post("/api/v1/parse", json={"message": "some message"})

            assert response.status_code == 500
            assert "Invalid message format" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_parse_unexpected_error_returns_500(self, app, mock_groq_client):
        """Test that unexpected exceptions return 500 with generic message."""
        with patch("routers.parse.parse_request", new_callable=AsyncMock) as mock_parse:
            mock_parse.side_effect = RuntimeError("Unexpected error")

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post("/api/v1/parse", json={"message": "some message"})

            assert response.status_code == 500
            assert "Internal server error" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_parse_non_request_message(self, app, mock_groq_client):
        """Test parsing a non-request message (like DJ message)."""
        non_request = make_parsed_request(
            is_request=False,
            message_type=MessageType.DJ_MESSAGE,
            raw_message="Thanks for listening!",
        )

        with patch("routers.parse.parse_request", new_callable=AsyncMock) as mock_parse:
            mock_parse.return_value = non_request

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/parse", json={"message": "Thanks for listening!"}
                )

            assert response.status_code == 200
            data = response.json()

            assert data["is_request"] is False
            assert data["message_type"] == "dj_message"
            assert data["song"] is None
            assert data["artist"] is None

    @pytest.mark.asyncio
    async def test_parse_feedback_message(self, app, mock_groq_client):
        """Test parsing a feedback message."""
        feedback = make_parsed_request(
            is_request=False,
            message_type=MessageType.FEEDBACK,
            raw_message="Love the show!",
        )

        with patch("routers.parse.parse_request", new_callable=AsyncMock) as mock_parse:
            mock_parse.return_value = feedback

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post("/api/v1/parse", json={"message": "Love the show!"})

            assert response.status_code == 200
            data = response.json()

            assert data["is_request"] is False
            assert data["message_type"] == "feedback"

    @pytest.mark.asyncio
    async def test_parse_missing_message_field(self, app, mock_groq_client):
        """Test that missing message field returns 422."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/v1/parse", json={})

        assert response.status_code == 422  # Validation error

    @pytest.mark.asyncio
    async def test_parse_wrong_message_type(self, app, mock_groq_client):
        """Test that wrong message type returns 422."""
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v1/parse",
                json={"message": 12345},  # Should be string
            )

        assert response.status_code == 422  # Validation error

    @pytest.mark.asyncio
    async def test_parse_song_with_artist(self, app, mock_groq_client):
        """Test parsing a song request with artist."""
        request = make_parsed_request(
            song="Stairway to Heaven",
            artist="Led Zeppelin",
            raw_message="Play Stairway to Heaven by Led Zeppelin",
        )

        with patch("routers.parse.parse_request", new_callable=AsyncMock) as mock_parse:
            mock_parse.return_value = request

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/parse", json={"message": "Play Stairway to Heaven by Led Zeppelin"}
                )

            assert response.status_code == 200
            data = response.json()

            assert data["song"] == "Stairway to Heaven"
            assert data["artist"] == "Led Zeppelin"
            assert data["album"] is None
            assert data["is_request"] is True

    @pytest.mark.asyncio
    async def test_parse_artist_only(self, app, mock_groq_client):
        """Test parsing a request with only artist."""
        request = make_parsed_request(
            artist="The Beatles",
            raw_message="Play something by The Beatles",
        )

        with patch("routers.parse.parse_request", new_callable=AsyncMock) as mock_parse:
            mock_parse.return_value = request

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/parse", json={"message": "Play something by The Beatles"}
                )

            assert response.status_code == 200
            data = response.json()

            assert data["song"] is None
            assert data["artist"] == "The Beatles"
            assert data["is_request"] is True
