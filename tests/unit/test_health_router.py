"""Unit tests for the routers/health.py wiring against wxyc_fastapi healthcheck.

The probe functions live locally in ``routers/health.py``; the routing and
status aggregation are imported from ``wxyc_fastapi.healthcheck``. These tests
pin the four observable response shapes the issue calls out:

* all probes ok -> 200 ``{"status": "healthy", "services": {...}}``
* required ``lookup`` probe fails -> 503 ``{"status": "unhealthy", ...}``
* optional ``groq`` probe fails -> 200 ``{"status": "degraded", ...}``
* optional ``slack`` probe fails -> 200 ``{"status": "degraded", ...}``
"""

from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from config.settings import Settings


def _make_settings(**overrides):
    return Settings(
        groq_api_key=overrides.pop("groq_api_key", "test_groq_key"),
        slack_webhook_url=overrides.pop("slack_webhook_url", "https://hooks.slack.com/test"),
        log_level=overrides.pop("log_level", "DEBUG"),
        enable_slack_integration=overrides.pop("enable_slack_integration", True),
        app_version=overrides.pop("app_version", "1.0.0-test"),
        lookup_service_url=overrides.pop("lookup_service_url", "https://lookup.example.com/api/v1"),
        **overrides,
    )


def _build_app(probe_results: dict[str, str]) -> FastAPI:
    """Wire a fresh FastAPI app with probes that return the supplied results.

    ``probe_results`` is a mapping like ``{"groq": "ok", "lookup": "ok", "slack": "ok"}``.
    Any probe whose result is not ``"ok"`` raises so we exercise the shared
    router's ``unavailable`` aggregation path.
    """
    from wxyc_fastapi.healthcheck import Check, liveness_router, readiness_router

    def _probe_factory(name: str):
        async def _probe() -> str:
            result = probe_results[name]
            if result == "ok":
                return "ok"
            raise RuntimeError(f"{name} probe simulated failure: {result}")

        return _probe

    checks = [
        Check(name="groq", probe=_probe_factory("groq"), required=False),
        Check(name="lookup", probe=_probe_factory("lookup"), required=True),
        Check(name="slack", probe=_probe_factory("slack"), required=False),
    ]

    app = FastAPI()
    app.include_router(liveness_router)
    app.include_router(readiness_router(checks))
    return app


class TestLiveness:
    @pytest.mark.asyncio
    async def test_returns_healthy_status(self):
        """``GET /health`` returns ``{"status": "healthy"}`` instantly, no probes run."""
        app = _build_app({"groq": "ok", "lookup": "ok", "slack": "ok"})

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            response = await c.get("/health")

        assert response.status_code == 200
        assert response.json() == {"status": "healthy"}


class TestReadinessShapes:
    @pytest.mark.asyncio
    async def test_all_probes_ok(self):
        """All probes ok -> 200 healthy with services map."""
        app = _build_app({"groq": "ok", "lookup": "ok", "slack": "ok"})

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            response = await c.get("/health/ready")

        assert response.status_code == 200
        assert response.json() == {
            "status": "healthy",
            "services": {"groq": "ok", "lookup": "ok", "slack": "ok"},
        }

    @pytest.mark.asyncio
    async def test_lookup_failure_is_unhealthy_503(self):
        """Required ``lookup`` probe failing -> 503 unhealthy."""
        app = _build_app({"groq": "ok", "lookup": "fail", "slack": "ok"})

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            response = await c.get("/health/ready")

        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "unhealthy"
        assert body["services"]["lookup"] == "unavailable"
        assert body["services"]["groq"] == "ok"
        assert body["services"]["slack"] == "ok"

    @pytest.mark.asyncio
    async def test_groq_failure_is_degraded_200(self):
        """Optional ``groq`` probe failing -> 200 degraded."""
        app = _build_app({"groq": "fail", "lookup": "ok", "slack": "ok"})

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            response = await c.get("/health/ready")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "degraded"
        assert body["services"]["groq"] == "unavailable"
        assert body["services"]["lookup"] == "ok"

    @pytest.mark.asyncio
    async def test_slack_failure_is_degraded_200(self):
        """Optional ``slack`` probe failing -> 200 degraded."""
        app = _build_app({"groq": "ok", "lookup": "ok", "slack": "fail"})

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            response = await c.get("/health/ready")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "degraded"
        assert body["services"]["slack"] == "unavailable"
        assert body["services"]["lookup"] == "ok"


class TestRomProbeWiring:
    """Tests for the rom-side probe functions wired into the shared router.

    These verify the rom probes still call the same upstream endpoints with
    the same auth they did before the migration -- only the routing/aggregation
    moved to wxyc_fastapi.
    """

    @pytest.mark.asyncio
    async def test_lookup_probe_posts_to_authenticated_endpoint(self):
        """Lookup probe POSTs to ``{lookup_service_url}/lookup`` with bearer auth."""
        import routers.health as health_module

        settings = _make_settings(lml_api_key="probe-token")
        client = AsyncMock()
        captured: list[tuple[str, dict]] = []

        async def _post(url, **kwargs):
            captured.append((url, dict(kwargs.get("headers") or {})))
            resp = Mock()
            resp.status_code = 200
            return resp

        client.post = _post

        result = await health_module.probe_lookup(settings, client)

        assert result == "ok"
        assert captured == [
            (
                "https://lookup.example.com/api/v1/lookup",
                {"Authorization": "Bearer probe-token"},
            )
        ]

    @pytest.mark.asyncio
    async def test_lookup_probe_marks_unavailable_on_401(self):
        """When LML returns 401 (auth misconfig), probe returns non-ok."""
        import routers.health as health_module

        settings = _make_settings(lml_api_key="wrong-token")
        client = AsyncMock()

        async def _post(url, **kwargs):
            resp = Mock()
            resp.status_code = 401
            return resp

        client.post = _post

        result = await health_module.probe_lookup(settings, client)

        assert result != "ok"

    @pytest.mark.asyncio
    async def test_lookup_probe_unavailable_when_url_unset(self):
        """No ``LOOKUP_SERVICE_URL`` -> probe returns non-ok (lookup is required)."""
        import routers.health as health_module

        settings = _make_settings(lookup_service_url=None)
        client = AsyncMock()

        result = await health_module.probe_lookup(settings, client)

        assert result != "ok"

    @pytest.mark.asyncio
    async def test_slack_probe_returns_ok_for_400_response(self):
        """Slack returning 400 (empty body rejected) is proof the webhook is alive."""
        import routers.health as health_module

        client = AsyncMock()

        async def _post(url, **kwargs):
            resp = Mock()
            resp.status_code = 400
            return resp

        client.post = _post

        with patch.object(
            health_module, "get_cached_slack_webhook_url", return_value="https://hooks.slack.com/x"
        ):
            result = await health_module.probe_slack(client)

        assert result == "ok"

    @pytest.mark.asyncio
    async def test_slack_probe_unavailable_when_webhook_unset(self):
        """No cached webhook URL -> probe returns non-ok."""
        import routers.health as health_module

        client = AsyncMock()

        with patch.object(health_module, "get_cached_slack_webhook_url", return_value=None):
            result = await health_module.probe_slack(client)

        assert result != "ok"

    @pytest.mark.asyncio
    async def test_groq_probe_unavailable_when_key_unset(self):
        """No ``GROQ_API_KEY`` -> probe returns non-ok."""
        import routers.health as health_module

        settings = _make_settings(groq_api_key="")
        client = AsyncMock()

        result = await health_module.probe_groq(settings, client)

        assert result != "ok"

    @pytest.mark.asyncio
    async def test_groq_probe_ok_for_200_response(self):
        """Groq returning 200 -> probe returns ok."""
        import routers.health as health_module

        settings = _make_settings()
        client = AsyncMock()

        async def _get(url, **kwargs):
            resp = Mock()
            resp.status_code = 200
            return resp

        client.get = _get

        result = await health_module.probe_groq(settings, client)

        assert result == "ok"
