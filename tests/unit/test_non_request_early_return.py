"""Tests for early return when parser classifies a message as not a request."""

from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from core.dependencies import (
    get_discogs_service,
    get_groq_client,
    get_library_db,
    get_posthog_client,
    get_slack_service,
)
from routers.request import router
from services.parser import MessageType, ParsedRequest


@pytest.fixture
def mock_groq_client():
    return Mock()


@pytest.fixture
def mock_library_db():
    db = AsyncMock()
    db.search = AsyncMock(return_value=[])
    return db


@pytest.fixture
def mock_discogs_service():
    return AsyncMock()


@pytest.fixture
def app(mock_groq_client, mock_library_db, mock_discogs_service):
    """Create a test app with all dependencies mocked."""
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    app.dependency_overrides[get_groq_client] = lambda: mock_groq_client
    app.dependency_overrides[get_library_db] = lambda: mock_library_db
    app.dependency_overrides[get_discogs_service] = lambda: mock_discogs_service
    app.dependency_overrides[get_slack_service] = lambda: None
    app.dependency_overrides[get_posthog_client] = lambda: None

    return app


class TestNonRequestEarlyReturn:
    """When the parser says is_request=False, the handler should skip the search pipeline."""

    @pytest.mark.asyncio
    async def test_feedback_message_returns_no_library_results(self, app, mock_library_db):
        """A feedback message like 'love the show!' should not trigger library search."""
        parsed = ParsedRequest(
            song=None,
            artist=None,
            album=None,
            is_request=False,
            message_type=MessageType.FEEDBACK,
            raw_message="love the show!",
        )

        with patch("routers.request.parse_request", return_value=parsed):
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
        # Should not have searched the library at all
        mock_library_db.search.assert_not_called()

    @pytest.mark.asyncio
    async def test_feedback_with_artist_does_not_search(self, app, mock_library_db):
        """Even if the parser extracts an artist from feedback, don't search.

        This is the 'I love acid, luke vibert' case: parser says feedback
        and extracts artist=Luke Vibert, but we should not search the library.
        """
        parsed = ParsedRequest(
            song=None,
            artist="Luke Vibert",
            album=None,
            is_request=False,
            message_type=MessageType.FEEDBACK,
            raw_message="I love acid, luke vibert",
        )

        with patch("routers.request.parse_request", return_value=parsed):
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
        mock_library_db.search.assert_not_called()

    @pytest.mark.asyncio
    async def test_dj_message_without_request_does_not_search(self, app, mock_library_db):
        """A DJ message that isn't a request should not trigger search."""
        parsed = ParsedRequest(
            song=None,
            artist=None,
            album=None,
            is_request=False,
            message_type=MessageType.DJ_MESSAGE,
            raw_message="you guys are great, keep it up",
        )

        with patch("routers.request.parse_request", return_value=parsed):
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
        mock_library_db.search.assert_not_called()

    @pytest.mark.asyncio
    async def test_actual_request_still_searches(self, app, mock_library_db):
        """Sanity check: an actual request should still go through the pipeline."""
        parsed = ParsedRequest(
            song="Bohemian Rhapsody",
            artist="Queen",
            album=None,
            is_request=True,
            message_type=MessageType.REQUEST,
            raw_message="play bohemian rhapsody by queen",
        )

        with patch("routers.request.parse_request", return_value=parsed):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play bohemian rhapsody by queen", "skip_slack": True},
                )

        assert response.status_code == 200
        # The search pipeline should have been invoked (even if it returned nothing)
        mock_library_db.search.assert_called()
