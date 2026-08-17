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
from services.parser import GROQ_MODEL


def _client_listing_models(*model_ids: str) -> AsyncMock:
    """An httpx client whose Groq ``/models`` call lists exactly ``model_ids``."""
    client = AsyncMock()

    async def _get(url, **kwargs):
        resp = Mock()
        resp.status_code = 200
        resp.json = Mock(return_value={"data": [{"id": mid} for mid in model_ids]})
        return resp

    client.get = _get
    return client


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
        """Flag off: Slack returning 400 (empty body rejected) is proof the webhook is alive."""
        import routers.health as health_module

        settings = _make_settings(slack_use_bot_token=False)
        client = AsyncMock()

        async def _post(url, **kwargs):
            resp = Mock()
            resp.status_code = 400
            return resp

        client.post = _post

        with patch.object(
            health_module, "get_cached_slack_webhook_url", return_value="https://hooks.slack.com/x"
        ):
            result = await health_module.probe_slack(settings, client)

        assert result == "ok"

    @pytest.mark.asyncio
    async def test_slack_probe_unavailable_when_webhook_unset(self):
        """Flag off, no cached webhook URL -> probe returns non-ok."""
        import routers.health as health_module

        settings = _make_settings(slack_use_bot_token=False)
        client = AsyncMock()

        with patch.object(health_module, "get_cached_slack_webhook_url", return_value=None):
            result = await health_module.probe_slack(settings, client)

        assert result != "ok"

    @pytest.mark.asyncio
    async def test_slack_probe_bot_token_ok_when_config_complete(self):
        """Flag on + bot token/channel both configured -> probe returns ok.

        Regression coverage for the readiness probe following the active
        transport (request-o-matic#219): before this, the probe always read
        ``get_cached_slack_webhook_url()``, which the bot-token transport
        deliberately never resolves, so this case used to report a permanent
        false ``unavailable``.
        """
        import routers.health as health_module

        settings = _make_settings(
            slack_use_bot_token=True,
            slack_bot_token="xoxb-test-token",
            slack_channel_id="C123",
        )
        client = AsyncMock()

        # The cached webhook URL is (correctly) never populated on this
        # transport; the probe must not consult it.
        with patch.object(health_module, "get_cached_slack_webhook_url", return_value=None):
            result = await health_module.probe_slack(settings, client)

        assert result == "ok"
        client.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_slack_probe_bot_token_unavailable_when_config_incomplete(self):
        """Flag on but bot token/channel not both set -> probe returns non-ok."""
        import routers.health as health_module

        settings = _make_settings(
            slack_use_bot_token=True,
            slack_bot_token=None,
            slack_channel_id="C123",
        )
        client = AsyncMock()

        result = await health_module.probe_slack(settings, client)

        assert result != "ok"

    @pytest.mark.asyncio
    async def test_slack_probe_bot_token_unavailable_when_integration_disabled(self):
        """``enable_slack_integration=False`` -> probe returns non-ok even with bot config set."""
        import routers.health as health_module

        settings = _make_settings(
            enable_slack_integration=False,
            slack_use_bot_token=True,
            slack_bot_token="xoxb-test-token",
            slack_channel_id="C123",
        )
        client = AsyncMock()

        result = await health_module.probe_slack(settings, client)

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
    async def test_groq_probe_ok_when_pinned_model_is_listed(self):
        """Groq reachable *and* still serving the pinned model -> ok."""
        import routers.health as health_module

        result = await health_module.probe_groq(
            _make_settings(), _client_listing_models(GROQ_MODEL, "llama-3.3-70b-versatile")
        )

        assert result == "ok"

    @pytest.mark.asyncio
    async def test_groq_probe_unavailable_when_pinned_model_is_missing(self):
        """A reachable Groq that no longer lists the pin is the 2026-08-17 incident.

        This is the only detector that fires without listener traffic. `/request`
        can only report a dead pin once someone sends a message the parser then
        fails to parse, and at this service's volume that took hours; readiness
        is polled on a schedule, so the same failure surfaces in zero requests.
        """
        import routers.health as health_module

        result = await health_module.probe_groq(
            _make_settings(), _client_listing_models("llama-3.3-70b-versatile")
        )

        assert result != "ok"

    @pytest.mark.asyncio
    async def test_groq_probe_unavailable_when_model_list_is_unreadable(self):
        """A 200 whose body is not the documented shape proves nothing about the pin."""
        import routers.health as health_module

        settings = _make_settings()
        client = AsyncMock()

        async def _get(url, **kwargs):
            resp = Mock()
            resp.status_code = 200
            resp.json = Mock(side_effect=ValueError("not json"))
            return resp

        client.get = _get

        result = await health_module.probe_groq(settings, client)

        assert result != "ok"

    @pytest.mark.asyncio
    async def test_groq_probe_stays_optional_so_a_dead_pin_cannot_pull_the_container(self):
        """`groq` is `required=False`, and this test is what keeps it that way.

        Tightening the probe makes it fail during a real outage, so the blast
        radius matters: parsing has a `parsing_unavailable` degraded mode and the
        listener's message still reaches Slack without it. If this check were
        required, a dead pin would turn `/health/ready` into a 503 and take a
        service that is still doing useful work out of rotation.
        """
        import routers.health as health_module

        app = FastAPI()
        with (
            patch.object(health_module, "get_http_client", AsyncMock(return_value=AsyncMock())),
            patch.object(health_module, "probe_groq", AsyncMock(return_value="unavailable")),
            patch.object(health_module, "probe_lookup", AsyncMock(return_value="ok")),
            patch.object(health_module, "probe_slack", AsyncMock(return_value="ok")),
        ):
            app.include_router(health_module.build_readiness_router())
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get("/health/ready")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "degraded"
        assert body["services"]["groq"] != "ok"
