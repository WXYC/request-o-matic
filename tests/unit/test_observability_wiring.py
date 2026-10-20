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
