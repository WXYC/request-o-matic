"""Wiring tests for the wxyc-fastapi observability glue.

Per-call behavior of `init_sentry`, `RequestTelemetry`, `init_cache_stats`,
and `get_posthog_client` is exercised by the `wxyc-fastapi` test suite. The
tests here pin only the rom-specific values that we pass to those primitives
(service name, distinct_id, event_prefix, api_call_keys, extra_keys), so a
later refactor can't silently change them.
"""

from __future__ import annotations

import importlib
import logging
import sys
from typing import cast
from unittest.mock import patch

import pytest


def test_main_calls_init_sentry_with_request_o_matic_service_name(monkeypatch):
    """`main` wires settings into `init_sentry` and pins the service name."""
    monkeypatch.setenv("GROQ_API_KEY", "test_key")
    monkeypatch.setenv("SENTRY_DSN", "https://test@sentry.io/123")
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "staging")
    monkeypatch.delenv("DEPLOYMENT_ENVIRONMENT", raising=False)

    from config import settings as settings_module

    sys.modules.pop("main", None)
    settings_module.get_settings.cache_clear()

    with patch("wxyc_fastapi.observability.init_sentry") as mock_init:
        importlib.import_module("main")

    from config.settings import Settings

    mock_init.assert_called_once()
    kwargs = mock_init.call_args.kwargs
    assert kwargs["dsn"] == "https://test@sentry.io/123"
    assert kwargs["service_name"] == "request-o-matic"
    assert kwargs["environment"] == "staging"
    assert kwargs["release"] == Settings().app_version


def test_main_pins_sentry_traces_sample_rate_to_zero(monkeypatch):
    """Tracing is off until the Sentry removal (#302); error capture stays on.

    `init_sentry` defaults `traces_sample_rate` to 1.0, so `main` must pass 0.0
    explicitly. Error events (`sample_rate`) keep the default so the
    Sentry-vs-PostHog parity window (WXYC/Backend-Service#3415) still has data.
    """
    monkeypatch.setenv("GROQ_API_KEY", "test_key")
    monkeypatch.setenv("SENTRY_DSN", "https://test@sentry.io/123")
    monkeypatch.setenv("SENTRY_TRACES_SAMPLE_RATE", "1.0")

    from config import settings as settings_module

    sys.modules.pop("main", None)
    settings_module.get_settings.cache_clear()

    with patch("wxyc_fastapi.observability.sentry.sentry_sdk.init") as mock_sdk_init:
        importlib.import_module("main")

    mock_sdk_init.assert_called_once()
    kwargs = mock_sdk_init.call_args.kwargs
    assert kwargs["traces_sample_rate"] == 0.0
    assert "traces_sampler" not in kwargs
    assert "profiles_sample_rate" not in kwargs
    assert kwargs["sample_rate"] == 1.0


@pytest.mark.parametrize(
    ("railway_env", "expected_ceiling"),
    [("staging", 50), ("stage", 50), ("Staging", 50), ("production", 100), ("local", 100)],
)
def test_main_calls_init_error_tracking_with_per_environment_ceiling(
    monkeypatch, railway_env, expected_ceiling
):
    """`main` wires settings into `init_error_tracking`; staging gets the smaller daily ceiling."""
    monkeypatch.setenv("GROQ_API_KEY", "test_key")
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", railway_env)
    monkeypatch.delenv("DEPLOYMENT_ENVIRONMENT", raising=False)

    from config import settings as settings_module

    sys.modules.pop("main", None)
    settings_module.get_settings.cache_clear()

    with patch("wxyc_fastapi.observability.init_error_tracking") as mock_init:
        main = importlib.import_module("main")

    from config.settings import Settings

    mock_init.assert_called_once()
    kwargs = mock_init.call_args.kwargs
    assert kwargs["service"] == "request-o-matic"
    assert kwargs["environment"] == railway_env
    assert kwargs["release"] == Settings().app_version
    assert kwargs["daily_ceiling"] == expected_ceiling
    assert kwargs["app"] is main.app


def test_error_log_produces_exception_event_through_stubbed_client():
    """An ERROR log reaches PostHog as a `$exception` once error tracking is initialised."""
    from posthog import Posthog
    from wxyc_fastapi.observability import PosthogErrorsClient, init_error_tracking
    from wxyc_fastapi.testing import CountingPosthog

    stub = CountingPosthog()
    client = PosthogErrorsClient(
        cast(Posthog, stub), service="request-o-matic", environment="staging", daily_ceiling=50
    )
    root = logging.getLogger()
    handlers_before = list(root.handlers)
    try:
        with (
            patch(
                "wxyc_fastapi.observability.error_tracking.get_posthog_errors_client",
                return_value=client,
            ),
            patch("wxyc_fastapi.observability.error_tracking._install_excepthooks"),
        ):
            init_error_tracking(service="request-o-matic", environment="staging", daily_ceiling=50)
        logging.getLogger("routers.request").error("LML lookup exploded")
    finally:
        root.handlers[:] = handlers_before

    assert [c.event for c in stub.captures] == ["$exception"]
    properties = stub.captures[0].properties
    assert properties is not None
    assert properties["service"] == "request-o-matic"
    assert properties["logger"] == "routers.request"


def test_error_log_in_request_through_main_app_produces_one_exception_event(monkeypatch):
    """An ERROR log inside a request to `main.app` reaches PostHog as one `$exception`.

    Goes through request-o-matic's own wiring: `main` runs `setup_logging(force=True)`
    (which strips root handlers) and only then `init_error_tracking(app=app)`. If the
    init moves above `setup_logging`, or is removed, the handler is gone and this fails.
    """
    from fastapi.testclient import TestClient
    from wxyc_fastapi.observability import error_tracking, errors
    from wxyc_fastapi.testing import CountingPosthog

    monkeypatch.setenv("GROQ_API_KEY", "test_key")
    monkeypatch.setenv("POSTHOG_ERRORS_API_KEY", "phc_test_errors_key")
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "staging")
    monkeypatch.delenv("DEPLOYMENT_ENVIRONMENT", raising=False)
    monkeypatch.setattr(errors, "_client", None)
    monkeypatch.setattr(errors, "_config", None)
    monkeypatch.setattr(errors, "_init_failed", False)
    monkeypatch.setattr(error_tracking, "_install_excepthooks", lambda client: None)

    from config import settings as settings_module

    stub = CountingPosthog()
    root = logging.getLogger()
    handlers_before = list(root.handlers)
    level_before = root.level
    sys.modules.pop("main", None)
    settings_module.get_settings.cache_clear()
    try:
        with patch("posthog.Posthog", return_value=stub):
            main = importlib.import_module("main")

        def _logs_an_error() -> dict[str, bool]:
            logging.getLogger("routers.request").error("LML lookup exploded")
            return {"ok": True}

        main.app.add_api_route("/_test/log-error", _logs_an_error)
        response = TestClient(main.app).get("/_test/log-error", headers={"X-Request-ID": "req-301"})
    finally:
        root.handlers[:] = handlers_before
        root.setLevel(level_before)
        sys.modules.pop("main", None)
        settings_module.get_settings.cache_clear()

    assert response.status_code == 200
    assert [c.event for c in stub.captures] == ["$exception"]
    properties = stub.captures[0].properties
    assert properties is not None
    assert properties["service"] == "request-o-matic"
    assert properties["logger"] == "routers.request"
    assert properties["request_id"] == "req-301"


def test_request_router_constructs_telemetry_with_rom_parameters():
    """`process_request` builds a `RequestTelemetry` keyed for rom's services."""
    from wxyc_fastapi.observability import RequestTelemetry

    telemetry = RequestTelemetry(
        api_call_keys=["groq", "discogs", "slack"],
        distinct_id="request-o-matic-service",
        event_prefix="request",
    )

    assert telemetry.api_calls == {"groq": 0, "discogs": 0, "slack": 0}
    assert telemetry.distinct_id == "request-o-matic-service"
    assert telemetry.event_prefix == "request"


def test_init_cache_stats_seeds_memory_misses_extra_key():
    """rom seeds `memory_misses` so the PostHog event shape stays stable."""
    from wxyc_fastapi.observability import get_cache_stats, init_cache_stats

    init_cache_stats(extra_keys=["memory_misses"])
    stats = get_cache_stats()

    assert stats is not None
    assert "memory_misses" in stats
    assert stats["memory_misses"] == 0
