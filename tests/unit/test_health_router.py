"""Unit tests for routers/health.py."""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from config.settings import Settings
from routers.health import router


def _make_settings(**overrides):
    return Settings(
        groq_api_key=overrides.pop("groq_api_key", "test_groq_key"),
        slack_webhook_url=overrides.pop("slack_webhook_url", "https://hooks.slack.com/test"),
        log_level=overrides.pop("log_level", "DEBUG"),
        enable_slack_integration=overrides.pop("enable_slack_integration", True),
        app_version=overrides.pop("app_version", "1.0.0-test"),
        **overrides,
    )


def _make_app(settings, http_client=None):
    """Build a FastAPI test app with dependency overrides."""
    from config.settings import get_settings
    from core.dependencies import get_http_client

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_http_client] = lambda: http_client or AsyncMock()
    return app


@pytest.fixture
def mock_settings():
    return _make_settings()


@pytest.fixture
def mock_http_client():
    """HTTP client that returns 200 for Groq and lookup probes, 400 for Slack."""
    client = AsyncMock()

    async def _get(url, **kwargs):
        resp = Mock()
        resp.status_code = 200
        return resp

    async def _post(url, **kwargs):
        resp = Mock()
        # Lookup probe expects 200; Slack expects 400 (empty body rejected).
        resp.status_code = 200 if "lookup" in url else 400
        return resp

    client.get = _get
    client.post = _post
    return client


class TestLivenessCheck:
    """Tests for the shallow /health liveness endpoint."""

    @pytest.mark.asyncio
    async def test_returns_ok(self):
        """Liveness check returns 200 immediately with no dependencies."""
        settings = _make_settings()
        app = _make_app(settings)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            response = await c.get("/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    @pytest.mark.asyncio
    async def test_no_external_calls(self):
        """Liveness check must not make any HTTP calls."""
        settings = _make_settings()
        client = AsyncMock()
        app = _make_app(settings, client)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            await c.get("/health")

        client.get.assert_not_called()
        client.post.assert_not_called()


class TestReadinessCheck:
    """Tests for the deep /health/ready readiness endpoint."""

    @pytest.mark.asyncio
    async def test_all_services_healthy(self, mock_http_client):
        """All services ok -> 200 healthy."""
        settings = _make_settings(
            lookup_service_url="https://lookup.example.com/api/v1",
        )
        app = _make_app(settings, mock_http_client)

        with patch(
            "routers.health.get_cached_slack_webhook_url",
            return_value="https://hooks.slack.com/test",
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                response = await c.get("/health/ready")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["version"] == "1.0.0-test"
        assert data["services"]["groq"] == "ok"
        assert data["services"]["lookup"] == "ok"
        assert data["services"]["slack"] == "ok"

    @pytest.mark.asyncio
    async def test_core_service_down_groq(self, mock_settings):
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
        app = _make_app(mock_settings, client)

        with patch(
            "routers.health.get_cached_slack_webhook_url",
            return_value="https://hooks.slack.com/test",
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                response = await c.get("/health/ready")

        assert response.status_code == 503
        data = response.json()
        assert data["status"] == "unhealthy"
        assert data["services"]["groq"] == "error"

    @pytest.mark.asyncio
    async def test_optional_service_down_degraded(self, mock_http_client):
        """Lookup service down, core ok -> 200 degraded."""
        settings = _make_settings(
            lookup_service_url="https://lookup.example.com/api/v1",
        )

        original_post = mock_http_client.post

        async def _post(url, **kwargs):
            if "lookup" in url:
                resp = Mock()
                resp.status_code = 500
                return resp
            return await original_post(url, **kwargs)

        mock_http_client.post = _post
        app = _make_app(settings, mock_http_client)

        with patch(
            "routers.health.get_cached_slack_webhook_url",
            return_value="https://hooks.slack.com/test",
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                response = await c.get("/health/ready")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "degraded"
        assert data["services"]["lookup"] == "error"
        assert data["services"]["groq"] == "ok"

    @pytest.mark.asyncio
    async def test_lookup_not_configured_unavailable(self, mock_settings, mock_http_client):
        """No LOOKUP_SERVICE_URL -> lookup 'unavailable', still healthy."""
        app = _make_app(mock_settings, mock_http_client)

        with patch(
            "routers.health.get_cached_slack_webhook_url",
            return_value="https://hooks.slack.com/test",
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                response = await c.get("/health/ready")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["services"]["lookup"] == "unavailable"

    @pytest.mark.asyncio
    async def test_slack_not_configured_unavailable(self, mock_settings, mock_http_client):
        """No cached Slack webhook URL -> 'unavailable', still healthy."""
        app = _make_app(mock_settings, mock_http_client)

        with patch("routers.health.get_cached_slack_webhook_url", return_value=None):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                response = await c.get("/health/ready")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["services"]["slack"] == "unavailable"

    @pytest.mark.asyncio
    async def test_timeout_reported(self, mock_settings):
        """A check that exceeds the timeout is reported as 'timeout'."""
        client = AsyncMock()

        async def _get(url, **kwargs):
            if "groq" in url:
                await asyncio.sleep(10)
            resp = Mock()
            resp.status_code = 200
            return resp

        async def _post(url, **kwargs):
            resp = Mock()
            resp.status_code = 400
            return resp

        client.get = _get
        client.post = _post
        app = _make_app(mock_settings, client)

        with (
            patch(
                "routers.health.get_cached_slack_webhook_url",
                return_value="https://hooks.slack.com/test",
            ),
            patch("routers.health.CHECK_TIMEOUT", 0.05),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                response = await c.get("/health/ready")

        assert response.status_code == 503
        data = response.json()
        assert data["services"]["groq"] == "timeout"
        assert data["status"] == "unhealthy"

    @pytest.mark.asyncio
    async def test_no_features_block(self, mock_settings, mock_http_client):
        """The response should not contain a 'features' block."""
        app = _make_app(mock_settings, mock_http_client)

        with patch(
            "routers.health.get_cached_slack_webhook_url",
            return_value="https://hooks.slack.com/test",
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                response = await c.get("/health/ready")

        data = response.json()
        assert "features" not in data

    @pytest.mark.asyncio
    async def test_lookup_probe_exercises_authenticated_endpoint(self):
        """Readiness lookup probe POSTs to /api/v1/lookup with a Bearer token.

        This catches LML auth misconfig: a missing or wrong LML_API_KEY produces
        a 401/403 from the protected route, which the probe must surface.
        Probing the unprotected /health endpoint would mask the failure.
        """
        settings = _make_settings(
            lookup_service_url="https://lookup.example.com/api/v1",
            lml_api_key="probe-token",
        )
        client = AsyncMock()
        captured_posts: list[tuple[str, dict]] = []

        async def _get(url, **kwargs):
            resp = Mock()
            resp.status_code = 200
            return resp

        async def _post(url, **kwargs):
            captured_posts.append((url, dict(kwargs.get("headers") or {})))
            resp = Mock()
            resp.status_code = 200 if "lookup" in url else 400
            return resp

        client.get = _get
        client.post = _post
        app = _make_app(settings, client)

        with patch(
            "routers.health.get_cached_slack_webhook_url",
            return_value="https://hooks.slack.com/test",
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                await c.get("/health/ready")

        lookup_posts = [(u, h) for (u, h) in captured_posts if "lookup" in u]
        assert lookup_posts, "expected POST to lookup endpoint"
        url, headers = lookup_posts[0]
        assert url == "https://lookup.example.com/api/v1/lookup"
        assert headers.get("Authorization") == "Bearer probe-token"

    @pytest.mark.asyncio
    async def test_lookup_probe_marks_error_on_401(self):
        """When LML returns 401 (auth misconfig), probe reports lookup='error'."""
        settings = _make_settings(
            lookup_service_url="https://lookup.example.com/api/v1",
            lml_api_key="wrong-token",
        )
        client = AsyncMock()

        async def _get(url, **kwargs):
            resp = Mock()
            resp.status_code = 200
            return resp

        async def _post(url, **kwargs):
            resp = Mock()
            resp.status_code = 401 if "lookup" in url else 400
            return resp

        client.get = _get
        client.post = _post
        app = _make_app(settings, client)

        with patch(
            "routers.health.get_cached_slack_webhook_url",
            return_value="https://hooks.slack.com/test",
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                response = await c.get("/health/ready")

        data = response.json()
        assert data["services"]["lookup"] == "error"

    @pytest.mark.asyncio
    async def test_multiple_optional_services_down(self):
        """Multiple optional services failing -> degraded, not unhealthy."""
        settings = _make_settings(
            lookup_service_url="https://lookup.example.com/api/v1",
        )
        client = AsyncMock()

        async def _get(url, **kwargs):
            resp = Mock()
            if "groq" in url:
                resp.status_code = 200
            else:
                resp.status_code = 500  # lookup down
            return resp

        async def _post(url, **kwargs):
            resp = Mock()
            resp.status_code = 500  # slack down
            return resp

        client.get = _get
        client.post = _post
        app = _make_app(settings, client)

        with patch(
            "routers.health.get_cached_slack_webhook_url",
            return_value="https://hooks.slack.com/test",
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                response = await c.get("/health/ready")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "degraded"
        assert data["services"]["lookup"] == "error"
        assert data["services"]["slack"] == "error"
