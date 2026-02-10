"""Unit tests for routers/health.py."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from config.settings import Settings
from routers.health import router


def _make_settings(**overrides):
    return Settings(
        groq_api_key=overrides.pop("groq_api_key", "test_groq_key"),
        discogs_token=overrides.pop("discogs_token", "test_discogs_token"),
        slack_webhook_url=overrides.pop("slack_webhook_url", "https://hooks.slack.com/test"),
        library_db_path=overrides.pop("library_db_path", Path("test_library.db")),
        log_level=overrides.pop("log_level", "DEBUG"),
        enable_slack_integration=overrides.pop("enable_slack_integration", True),
        enable_artwork_lookup=overrides.pop("enable_artwork_lookup", True),
        app_version=overrides.pop("app_version", "1.0.0-test"),
        **overrides,
    )


def _make_app(settings, db, discogs_service, http_client=None):
    """Build a FastAPI test app with dependency overrides."""
    from config.settings import get_settings
    from core.dependencies import get_discogs_service, get_http_client, get_library_db

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_library_db] = lambda: db
    app.dependency_overrides[get_discogs_service] = lambda: discogs_service
    app.dependency_overrides[get_http_client] = lambda: http_client or AsyncMock()
    return app


@pytest.fixture
def mock_settings():
    return _make_settings()


@pytest.fixture
def mock_library_db():
    db = AsyncMock()
    db.is_available = AsyncMock(return_value=True)
    return db


@pytest.fixture
def mock_discogs_service():
    svc = AsyncMock()
    svc.check_api = AsyncMock(return_value=True)
    svc.cache_service = AsyncMock()
    svc.cache_service.is_available = AsyncMock(return_value=True)
    return svc


@pytest.fixture
def mock_http_client():
    """HTTP client that returns 200 for Groq and 400 for Slack."""
    client = AsyncMock()

    async def _get(url, **kwargs):
        resp = Mock()
        resp.status_code = 200
        return resp

    async def _post(url, **kwargs):
        resp = Mock()
        resp.status_code = 400  # Slack returns 400 for empty body
        return resp

    client.get = _get
    client.post = _post
    return client


class TestHealthCheck:
    """Tests for the health check endpoint."""

    @pytest.mark.asyncio
    async def test_all_services_healthy(
        self, mock_settings, mock_library_db, mock_discogs_service, mock_http_client
    ):
        """All services ok -> 200 healthy."""
        app = _make_app(mock_settings, mock_library_db, mock_discogs_service, mock_http_client)

        with patch(
            "routers.health.get_cached_slack_webhook_url",
            return_value="https://hooks.slack.com/test",
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                response = await c.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["version"] == "1.0.0-test"
        assert data["services"]["groq"] == "ok"
        assert data["services"]["database"] == "ok"
        assert data["services"]["discogs_api"] == "ok"
        assert data["services"]["discogs_cache"] == "ok"
        assert data["services"]["slack"] == "ok"

    @pytest.mark.asyncio
    async def test_core_service_down_database(
        self, mock_settings, mock_discogs_service, mock_http_client
    ):
        """Database down -> 503 unhealthy."""
        db = AsyncMock()
        db.is_available = AsyncMock(return_value=False)
        app = _make_app(mock_settings, db, mock_discogs_service, mock_http_client)

        with patch(
            "routers.health.get_cached_slack_webhook_url",
            return_value="https://hooks.slack.com/test",
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                response = await c.get("/health")

        assert response.status_code == 503
        data = response.json()
        assert data["status"] == "unhealthy"
        assert data["services"]["database"] == "error"

    @pytest.mark.asyncio
    async def test_core_service_down_groq(
        self, mock_settings, mock_library_db, mock_discogs_service
    ):
        """Groq returning non-200 -> 503 unhealthy."""
        client = AsyncMock()

        async def _get(url, **kwargs):
            resp = Mock()
            resp.status_code = 401  # Bad API key
            return resp

        async def _post(url, **kwargs):
            resp = Mock()
            resp.status_code = 400
            return resp

        client.get = _get
        client.post = _post
        app = _make_app(mock_settings, mock_library_db, mock_discogs_service, client)

        with patch(
            "routers.health.get_cached_slack_webhook_url",
            return_value="https://hooks.slack.com/test",
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                response = await c.get("/health")

        assert response.status_code == 503
        data = response.json()
        assert data["status"] == "unhealthy"
        assert data["services"]["groq"] == "error"

    @pytest.mark.asyncio
    async def test_optional_service_down_degraded(
        self, mock_settings, mock_library_db, mock_http_client
    ):
        """Discogs API down, core ok -> 200 degraded."""
        discogs = AsyncMock()
        discogs.check_api = AsyncMock(return_value=False)
        discogs.cache_service = AsyncMock()
        discogs.cache_service.is_available = AsyncMock(return_value=True)

        app = _make_app(mock_settings, mock_library_db, discogs, mock_http_client)

        with patch(
            "routers.health.get_cached_slack_webhook_url",
            return_value="https://hooks.slack.com/test",
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                response = await c.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "degraded"
        assert data["services"]["discogs_api"] == "error"
        # Core services still ok
        assert data["services"]["groq"] == "ok"
        assert data["services"]["database"] == "ok"

    @pytest.mark.asyncio
    async def test_discogs_not_configured_unavailable(
        self, mock_settings, mock_library_db, mock_http_client
    ):
        """No Discogs service -> both discogs_api and discogs_cache are 'unavailable'."""
        app = _make_app(mock_settings, mock_library_db, None, mock_http_client)

        with patch(
            "routers.health.get_cached_slack_webhook_url",
            return_value="https://hooks.slack.com/test",
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                response = await c.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["services"]["discogs_api"] == "unavailable"
        assert data["services"]["discogs_cache"] == "unavailable"

    @pytest.mark.asyncio
    async def test_slack_not_configured_unavailable(
        self, mock_settings, mock_library_db, mock_discogs_service, mock_http_client
    ):
        """No cached Slack webhook URL -> 'unavailable', still healthy."""
        app = _make_app(mock_settings, mock_library_db, mock_discogs_service, mock_http_client)

        with patch("routers.health.get_cached_slack_webhook_url", return_value=None):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                response = await c.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["services"]["slack"] == "unavailable"

    @pytest.mark.asyncio
    async def test_discogs_cache_unavailable_when_no_cache_service(
        self, mock_settings, mock_library_db, mock_http_client
    ):
        """Discogs configured but no cache service -> discogs_cache 'unavailable'."""
        discogs = AsyncMock()
        discogs.check_api = AsyncMock(return_value=True)
        discogs.cache_service = None

        app = _make_app(mock_settings, mock_library_db, discogs, mock_http_client)

        with patch(
            "routers.health.get_cached_slack_webhook_url",
            return_value="https://hooks.slack.com/test",
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                response = await c.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["services"]["discogs_api"] == "ok"
        assert data["services"]["discogs_cache"] == "unavailable"

    @pytest.mark.asyncio
    async def test_timeout_reported(self, mock_settings, mock_discogs_service, mock_http_client):
        """A check that exceeds the timeout is reported as 'timeout'."""
        db = AsyncMock()

        async def _slow_check():
            await asyncio.sleep(10)
            return True

        db.is_available = _slow_check

        app = _make_app(mock_settings, db, mock_discogs_service, mock_http_client)

        with (
            patch(
                "routers.health.get_cached_slack_webhook_url",
                return_value="https://hooks.slack.com/test",
            ),
            patch("routers.health.CHECK_TIMEOUT", 0.05),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                response = await c.get("/health")

        assert response.status_code == 503
        data = response.json()
        assert data["services"]["database"] == "timeout"
        assert data["status"] == "unhealthy"

    @pytest.mark.asyncio
    async def test_no_features_block(
        self, mock_settings, mock_library_db, mock_discogs_service, mock_http_client
    ):
        """The response should not contain a 'features' block."""
        app = _make_app(mock_settings, mock_library_db, mock_discogs_service, mock_http_client)

        with patch(
            "routers.health.get_cached_slack_webhook_url",
            return_value="https://hooks.slack.com/test",
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                response = await c.get("/health")

        data = response.json()
        assert "features" not in data

    @pytest.mark.asyncio
    async def test_multiple_optional_services_down(
        self, mock_settings, mock_library_db, mock_http_client
    ):
        """Multiple optional services failing -> degraded, not unhealthy."""
        discogs = AsyncMock()
        discogs.check_api = AsyncMock(return_value=False)
        discogs.cache_service = AsyncMock()
        discogs.cache_service.is_available = AsyncMock(return_value=False)

        app = _make_app(mock_settings, mock_library_db, discogs, mock_http_client)

        # Slack also down (webhook returns 500)
        async def _post(url, **kwargs):
            resp = Mock()
            resp.status_code = 500
            return resp

        mock_http_client.post = _post

        with patch(
            "routers.health.get_cached_slack_webhook_url",
            return_value="https://hooks.slack.com/test",
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                response = await c.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "degraded"
        assert data["services"]["discogs_api"] == "error"
        assert data["services"]["discogs_cache"] == "error"
        assert data["services"]["slack"] == "error"
