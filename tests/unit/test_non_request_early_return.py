"""Tests for early return when parser classifies a message as not a request."""

from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from core.dependencies import (
    get_groq_client,
    get_lookup_client,
    get_posthog_client,
    get_slack_service,
)
from routers.request import router
from services.parser import MessageType
from tests.conftest import make_parsed_request


@pytest.fixture
def mock_groq_client():
    return Mock()


@pytest.fixture
def app(mock_groq_client):
    """Create a test app with all dependencies mocked.

    No lookup_client is provided, so actual requests will get 503.
    Non-requests early-return before the lookup_client check.
    """
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    app.dependency_overrides[get_groq_client] = lambda: mock_groq_client
    app.dependency_overrides[get_slack_service] = lambda: None
    app.dependency_overrides[get_posthog_client] = lambda: None
    app.dependency_overrides[get_lookup_client] = lambda: None

    return app


class TestNonRequestEarlyReturn:
    """When the parser says is_request=False, the handler should skip the search pipeline."""

    @pytest.mark.asyncio
    async def test_feedback_message_returns_no_library_results(self, app):
        """A feedback message like 'love the show!' should not trigger library search."""
        parsed = make_parsed_request(
            is_request=False,
            message_type=MessageType.FEEDBACK,
            raw_message="love the show!",
        )

        with patch("routers.request.parse_request", new_callable=AsyncMock, return_value=parsed):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "love the show!", "skip_slack": True},
                )

        assert response.status_code == 200
        data = response.json()
        assert data["parsed"]["is_request"] is False
        assert data["library_results"] == []

    @pytest.mark.asyncio
    async def test_feedback_with_artist_does_not_search(self, app):
        """Even if the parser extracts an artist from feedback, don't search.

        This is the 'I love acid, luke vibert' case: parser says feedback
        and extracts artist=Luke Vibert, but we should not search the library.
        """
        parsed = make_parsed_request(
            artist="Luke Vibert",
            is_request=False,
            message_type=MessageType.FEEDBACK,
            raw_message="I love acid, luke vibert",
        )

        with patch("routers.request.parse_request", new_callable=AsyncMock, return_value=parsed):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "I love acid, luke vibert", "skip_slack": True},
                )

        assert response.status_code == 200
        data = response.json()
        assert data["parsed"]["is_request"] is False
        assert data["parsed"]["artist"] == "Luke Vibert"
        assert data["library_results"] == []

    @pytest.mark.asyncio
    async def test_dj_message_without_request_does_not_search(self, app):
        """A DJ message that isn't a request should not trigger search."""
        parsed = make_parsed_request(
            is_request=False,
            message_type=MessageType.DJ_MESSAGE,
            raw_message="you guys are great, keep it up",
        )

        with patch("routers.request.parse_request", new_callable=AsyncMock, return_value=parsed):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "you guys are great, keep it up", "skip_slack": True},
                )

        assert response.status_code == 200
        data = response.json()
        assert data["parsed"]["is_request"] is False
        assert data["library_results"] == []

    @pytest.mark.asyncio
    async def test_actual_request_degrades_without_lookup_client(self, app):
        """A request with no lookup service configured falls back to search-unavailable."""
        parsed = make_parsed_request(
            song="la paradoja",
            artist="Juana Molina",
            raw_message="play la paradoja by juana molina",
        )

        with patch("routers.request.parse_request", new_callable=AsyncMock, return_value=parsed):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play la paradoja by juana molina", "skip_slack": True},
                )

        assert response.status_code == 200
        data = response.json()
        assert data["degraded_mode"] == "search_unavailable"
        assert data["library_results"] == []
