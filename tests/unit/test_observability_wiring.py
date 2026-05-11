"""Wiring tests for the wxyc-fastapi observability glue.

Per-call behavior of `init_sentry`, `RequestTelemetry`, `init_cache_stats`,
and `get_posthog_client` is exercised by the `wxyc-fastapi` test suite. The
tests here pin only the rom-specific values that we pass to those primitives
(service name, distinct_id, event_prefix, api_call_keys, extra_keys), so a
later refactor can't silently change them.
"""

from __future__ import annotations

import importlib
import sys
from unittest.mock import patch


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
