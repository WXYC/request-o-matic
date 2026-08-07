"""Unit tests for the User-Agent gate (WXYC/request-o-matic#155).

Covers two surfaces:

* ``services.ua_gate.is_known_strict_client`` — pure-function matcher for
  product-token + minimum-version pairs in the User-Agent header.
* The router wiring in ``handle_request`` — when
  ``STRICT_FINGERPRINT_FOR_KNOWN_CLIENTS=true``, a known-client request that
  omits ``X-Device-Fingerprint`` is rejected with 403 before parse, before
  the BS ban check, and without consuming Groq/LML budget.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from config.settings import Settings, get_settings
from core.dependencies import (
    get_ban_check_client,
    get_groq_client,
    get_lookup_client,
    get_posthog_client,
    get_slack_service,
)
from generated.api_models import CacheStats, SearchType
from routers.request import router
from services.ban_check_client import BanCheckClient
from services.lookup_client import LookupResponse, LookupResult, LookupServiceClient
from services.ua_gate import (
    UA_GATE_BAN_REASON,
    UA_GATE_BAN_SOURCE,
    UA_GATE_MALFORMED_BAN_REASON,
    is_known_strict_client,
)
from tests.conftest import make_parsed_request

SAMPLE_PARSED = make_parsed_request(
    song="la paradoja",
    artist="Juana Molina",
    raw_message="play la paradoja by juana molina",
)

EMPTY_LOOKUP = LookupResponse(
    results=[],
    search_type=SearchType.none,
    song_not_found=False,
    found_on_compilation=False,
    context_message=None,
    corrected_artist=None,
    cache_stats=CacheStats(
        memory_hits=0,
        pg_hits=0,
        pg_misses=0,
        api_calls=0,
        pg_time_ms=0.0,
        api_time_ms=0.0,
    ),
)


# ---------------------------------------------------------------------------
# Pure matcher: services.ua_gate.is_known_strict_client
# ---------------------------------------------------------------------------


class TestUAMatcher:
    """Behavior of ``is_known_strict_client`` against every UA category we expect."""

    @pytest.mark.parametrize(
        "ua",
        [
            None,
            "",
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) Safari/605.1.15",
            "curl/8.6.0",
            "python-requests/2.31.0",
            "PostmanRuntime/7.36.1",
        ],
        ids=["none", "empty", "browser", "curl", "requests", "postman"],
    )
    def test_unknown_uas_return_false(self, ua: str | None) -> None:
        assert is_known_strict_client(ua) is False

    @pytest.mark.parametrize(
        "ua",
        [
            "WXYC-iOS/3.2",
            "WXYC-iOS/3.2.0",
            "WXYC-iOS/3.2.1",
            "WXYC-iOS/3.3",
            "WXYC-iOS/3.10.0",
            "WXYC-iOS/4.0.0",
            # URLSession-style with trailing CFNetwork/Darwin tokens
            "WXYC-iOS/3.2.0 CFNetwork/1568.300.101 Darwin/24.4.0",
            "WXYC-iOS/3.2.0 (Build 103)",
        ],
        ids=[
            "v3.2-bare",
            "v3.2.0",
            "v3.2.1",
            "v3.3",
            "v3.10.0",
            "v4.0.0",
            "v3.2.0-with-cfnetwork",
            "v3.2.0-with-build",
        ],
    )
    def test_known_strict_client_returns_true(self, ua: str) -> None:
        assert is_known_strict_client(ua) is True

    @pytest.mark.parametrize(
        "ua",
        [
            "WXYC-iOS/3.0",
            "WXYC-iOS/3.0.0",
            "WXYC-iOS/3.1",
            "WXYC-iOS/3.1.99",
            "WXYC-iOS/2.0",
            "WXYC-iOS/1.5",
            # Version 3 without a minor — Python's tuple-compare treats (3,) < (3, 2)
            "WXYC-iOS/3",
        ],
        ids=["v3.0", "v3.0.0", "v3.1", "v3.1.99", "v2.0", "v1.5", "v3-bare"],
    )
    def test_below_minimum_returns_false(self, ua: str) -> None:
        assert is_known_strict_client(ua) is False

    def test_malformed_version_returns_false(self) -> None:
        # The product-token regex restricts to dotted-numeric so this case is
        # already filtered upstream, but the guard inside the function is the
        # one that protects against a future regex loosening.
        assert is_known_strict_client("WXYC-iOS/three-point-two") is False
        assert is_known_strict_client("WXYC-iOS/v3.2") is False

    def test_unknown_product_returns_false(self) -> None:
        # WXYC-Android is not in the registry yet — its absence keeps Android
        # traffic on the lenient path until that team ships ban-aware code.
        assert is_known_strict_client("WXYC-Android/3.2.0") is False
        # Capitalization is exact-match: a stray "wxyc-ios" lowercase doesn't
        # impersonate the real client.
        assert is_known_strict_client("wxyc-ios/3.2.0") is False

    def test_mixed_tokens_picks_up_known_one(self) -> None:
        # Real iOS UAs are space-separated product tokens; the matcher should
        # find the WXYC-iOS token even when other tokens are present.
        ua = "Some-Wrapper/1.0 WXYC-iOS/3.2.0 (Build 103) CFNetwork/1568.300.101"
        assert is_known_strict_client(ua) is True

    def test_first_unknown_token_does_not_short_circuit(self) -> None:
        # An earlier unregistered product (Mozilla, etc.) must not prevent us
        # from finding a later WXYC-iOS token.
        assert is_known_strict_client("Mozilla/5.0 WXYC-iOS/3.2.0") is True


# ---------------------------------------------------------------------------
# Router wiring: the gate fires before the BS ban check
# ---------------------------------------------------------------------------


def _make_app(
    *,
    strict_flag: bool,
    ban_check_client: BanCheckClient | None = None,
    lookup_client: LookupServiceClient | None = None,
    slack_service: AsyncMock | None = None,
    posthog_client: Mock | None = None,
) -> FastAPI:
    """Construct a fresh app with the UA-gate flag set to ``strict_flag``."""
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    test_settings = Settings(
        groq_api_key="test_groq_key",
        strict_fingerprint_for_known_clients=strict_flag,
    )
    app.dependency_overrides[get_settings] = lambda: test_settings
    app.dependency_overrides[get_groq_client] = lambda: Mock()
    app.dependency_overrides[get_slack_service] = lambda: slack_service
    app.dependency_overrides[get_posthog_client] = lambda: posthog_client
    app.dependency_overrides[get_lookup_client] = lambda: lookup_client
    app.dependency_overrides[get_ban_check_client] = lambda: ban_check_client
    return app


@pytest.fixture
def mock_ban_check_client():
    return AsyncMock(spec=BanCheckClient)


@pytest.fixture
def mock_lookup_client():
    client = AsyncMock(spec=LookupServiceClient)
    client.lookup.return_value = LookupResult(response=EMPTY_LOOKUP, server_timing=None)
    return client


@pytest.fixture
def mock_slack_service():
    svc = AsyncMock()
    svc.post_blocks = AsyncMock()
    svc.webhook_url = "https://hooks.slack.com/test"
    return svc


@pytest.fixture
def mock_posthog():
    posthog = Mock()
    posthog.capture = Mock()
    return posthog


class TestUAGateBlocks:
    """With the flag on, known-client requests missing a usable fingerprint get 403."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "fingerprint",
        [
            pytest.param("not-a-uuid", id="malformed"),
            pytest.param("", id="empty"),
            pytest.param("   ", id="whitespace-only"),
            pytest.param("abc?inject=evil", id="query-injection"),
        ],
    )
    async def test_known_client_unusable_fingerprint_returns_403(
        self,
        mock_ban_check_client,
        mock_lookup_client,
        mock_slack_service,
        mock_posthog,
        fingerprint,
    ):
        """A present-but-unusable fingerprint is evasion, not a fingerprint.

        The gate exists so a known client can't opt out of ban enforcement by
        withholding its fingerprint. Checking mere header *presence* left the
        door open one notch further: a spoofed ``WXYC-iOS/3.2.0`` sending
        ``X-Device-Fingerprint: not-a-uuid`` and no ``Authorization`` cleared
        the gate on presence, then normalized to no-signal and skipped the BS
        round-trip entirely -- passing both defenses. That is precisely the
        bypass ``services/ua_gate.py`` claims cannot happen ("has to then also
        send a fingerprint, at which point they're back on the
        fingerprint-banned path").
        """
        app = _make_app(
            strict_flag=True,
            ban_check_client=mock_ban_check_client,
            lookup_client=mock_lookup_client,
            slack_service=mock_slack_service,
            posthog_client=mock_posthog,
        )

        with patch(
            "routers.request.parse_request",
            new_callable=AsyncMock,
            return_value=SAMPLE_PARSED,
        ) as mock_parse:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play la paradoja"},
                    headers={
                        "User-Agent": "WXYC-iOS/3.2.0",
                        "X-Device-Fingerprint": fingerprint,
                    },
                )

        assert response.status_code == 403
        mock_ban_check_client.check.assert_not_awaited()
        mock_parse.assert_not_awaited()
        mock_lookup_client.lookup.assert_not_awaited()
        mock_slack_service.post_blocks.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_known_client_unusable_fingerprint_with_jwt_still_403(
        self,
        mock_ban_check_client,
        mock_lookup_client,
        mock_slack_service,
        mock_posthog,
    ):
        """A valid ``Authorization`` header does not excuse a garbage fingerprint.

        The gate has always ignored ``Authorization`` -- a known client missing
        its fingerprint is 403'd even holding a good JWT -- so a known client
        sending a *malformed* one must land the same way. Pinning this keeps a
        future "but they're authenticated, let them through" softening from
        silently reopening the evasion path, since the attacker controls which
        headers they send.
        """
        app = _make_app(
            strict_flag=True,
            ban_check_client=mock_ban_check_client,
            lookup_client=mock_lookup_client,
            slack_service=mock_slack_service,
            posthog_client=mock_posthog,
        )

        with patch(
            "routers.request.parse_request",
            new_callable=AsyncMock,
            return_value=SAMPLE_PARSED,
        ) as mock_parse:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play la paradoja"},
                    headers={
                        "User-Agent": "WXYC-iOS/3.2.0",
                        "X-Device-Fingerprint": "not-a-uuid",
                        "Authorization": "Bearer eyJ...",
                    },
                )

        assert response.status_code == 403
        mock_ban_check_client.check.assert_not_awaited()
        mock_parse.assert_not_awaited()
        mock_lookup_client.lookup.assert_not_awaited()
        mock_slack_service.post_blocks.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_known_client_no_fingerprint_returns_403(
        self,
        mock_ban_check_client,
        mock_lookup_client,
        mock_slack_service,
        mock_posthog,
    ):
        app = _make_app(
            strict_flag=True,
            ban_check_client=mock_ban_check_client,
            lookup_client=mock_lookup_client,
            slack_service=mock_slack_service,
            posthog_client=mock_posthog,
        )

        with patch(
            "routers.request.parse_request",
            new_callable=AsyncMock,
            return_value=SAMPLE_PARSED,
        ) as mock_parse:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play la paradoja"},
                    headers={"User-Agent": "WXYC-iOS/3.2.0"},
                )

        assert response.status_code == 403
        # Reject BEFORE both BS round-trip and parse.
        mock_ban_check_client.check.assert_not_awaited()
        mock_parse.assert_not_awaited()
        mock_lookup_client.lookup.assert_not_awaited()
        mock_slack_service.post_blocks.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_blocked_request_emits_posthog_event_with_ua_source(
        self,
        mock_ban_check_client,
        mock_lookup_client,
        mock_slack_service,
        mock_posthog,
    ):
        app = _make_app(
            strict_flag=True,
            ban_check_client=mock_ban_check_client,
            lookup_client=mock_lookup_client,
            slack_service=mock_slack_service,
            posthog_client=mock_posthog,
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.post(
                "/api/v1/request",
                json={"message": "play la paradoja"},
                headers={"User-Agent": "WXYC-iOS/3.2.0"},
            )

        mock_posthog.capture.assert_called_once()
        call_kwargs = mock_posthog.capture.call_args.kwargs
        assert call_kwargs["event"] == "request_blocked"
        # ban_source distinguishes UA-gate rejection (rom_strict_mode) from a
        # real BS ban (bs); ops dashboards key off this property.
        assert call_kwargs["properties"]["ban_source"] == UA_GATE_BAN_SOURCE
        assert call_kwargs["properties"]["ban_reason"] == UA_GATE_BAN_REASON
        assert call_kwargs["properties"]["user_agent"] == "WXYC-iOS/3.2.0"
        # Missing header: nothing to describe, so neither property is set
        # (WXYC/request-o-matic#226) -- no permanent null column on every
        # legitimately-absent-header rejection.
        assert "fingerprint_prefix" not in call_kwargs["properties"]
        assert "fingerprint_length" not in call_kwargs["properties"]

    @pytest.mark.asyncio
    async def test_malformed_header_emits_distinct_reason_and_redacted_prefix(
        self,
        mock_ban_check_client,
        mock_lookup_client,
        mock_slack_service,
        mock_posthog,
    ):
        """A present-but-unusable fingerprint gets its own ban_reason plus a
        bounded, redacted indication of what arrived -- distinguishing an
        active evader from a client that simply omitted the header
        (WXYC/request-o-matic#226).
        """
        app = _make_app(
            strict_flag=True,
            ban_check_client=mock_ban_check_client,
            lookup_client=mock_lookup_client,
            slack_service=mock_slack_service,
            posthog_client=mock_posthog,
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v1/request",
                json={"message": "play la paradoja"},
                headers={
                    "User-Agent": "WXYC-iOS/3.2.0",
                    "X-Device-Fingerprint": "not-a-uuid-but-quite-long-garbage",
                },
            )

        assert response.status_code == 403
        mock_posthog.capture.assert_called_once()
        properties = mock_posthog.capture.call_args.kwargs["properties"]
        assert properties["ban_reason"] == UA_GATE_MALFORMED_BAN_REASON
        # fingerprint stays None -- existing queries against it keep working.
        assert properties["fingerprint"] is None
        # Bounded to 8 chars and never the raw value verbatim.
        assert properties["fingerprint_prefix"] == "not-a-uu"
        assert len(properties["fingerprint_prefix"]) == 8
        assert properties["fingerprint_length"] == len("not-a-uuid-but-quite-long-garbage")

    @pytest.mark.asyncio
    async def test_empty_header_is_treated_as_malformed_not_missing(
        self,
        mock_ban_check_client,
        mock_lookup_client,
        mock_slack_service,
        mock_posthog,
    ):
        # FastAPI binds a present-but-empty header to "", not None -- that is a
        # value that arrived, distinct from the header being omitted entirely.
        app = _make_app(
            strict_flag=True,
            ban_check_client=mock_ban_check_client,
            lookup_client=mock_lookup_client,
            slack_service=mock_slack_service,
            posthog_client=mock_posthog,
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.post(
                "/api/v1/request",
                json={"message": "play la paradoja"},
                headers={"User-Agent": "WXYC-iOS/3.2.0", "X-Device-Fingerprint": ""},
            )

        properties = mock_posthog.capture.call_args.kwargs["properties"]
        assert properties["ban_reason"] == UA_GATE_MALFORMED_BAN_REASON
        assert properties["fingerprint_prefix"] == ""
        assert properties["fingerprint_length"] == 0

    @pytest.mark.asyncio
    async def test_posthog_capture_failure_does_not_flip_response(
        self,
        mock_ban_check_client,
        mock_lookup_client,
        mock_slack_service,
        mock_posthog,
    ):
        # Shadow-ban invariant: a PostHog ingest outage must not 500 the
        # response. The 403 has to land regardless.
        mock_posthog.capture.side_effect = RuntimeError("posthog down")
        app = _make_app(
            strict_flag=True,
            ban_check_client=mock_ban_check_client,
            lookup_client=mock_lookup_client,
            slack_service=mock_slack_service,
            posthog_client=mock_posthog,
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/api/v1/request",
                json={"message": "play la paradoja"},
                headers={"User-Agent": "WXYC-iOS/3.2.0"},
            )

        assert response.status_code == 403


class TestUAGatePasses:
    """With the flag on, requests that don't match the gate fall through."""

    @pytest.mark.asyncio
    async def test_known_client_with_fingerprint_passes_to_ban_check(
        self,
        mock_ban_check_client,
        mock_lookup_client,
        mock_slack_service,
        mock_posthog,
    ):
        # ban_check_client present + headers present → BS is consulted.
        # Returning banned=False lets the request proceed; we just want to see
        # the gate did NOT short-circuit before reaching BS.
        from services.ban_check_client import BanCheckResult

        mock_ban_check_client.check.return_value = BanCheckResult(banned=False)
        app = _make_app(
            strict_flag=True,
            ban_check_client=mock_ban_check_client,
            lookup_client=mock_lookup_client,
            slack_service=mock_slack_service,
            posthog_client=mock_posthog,
        )

        with patch(
            "routers.request.parse_request",
            new_callable=AsyncMock,
            return_value=SAMPLE_PARSED,
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play la paradoja"},
                    headers={
                        "User-Agent": "WXYC-iOS/3.2.0",
                        "X-Device-Fingerprint": "11111111-2222-4333-8444-555555555555",
                    },
                )

        assert response.status_code == 200
        mock_ban_check_client.check.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unknown_client_no_fingerprint_passes_through(
        self,
        mock_lookup_client,
        mock_slack_service,
        mock_posthog,
    ):
        # No ban_check_client (BS flag off too), unknown UA, no fingerprint.
        # The gate should not fire and the request should proceed.
        app = _make_app(
            strict_flag=True,
            ban_check_client=None,
            lookup_client=mock_lookup_client,
            slack_service=mock_slack_service,
            posthog_client=mock_posthog,
        )

        with patch(
            "routers.request.parse_request",
            new_callable=AsyncMock,
            return_value=SAMPLE_PARSED,
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play la paradoja"},
                    headers={"User-Agent": "curl/8.6.0"},
                )

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_below_minimum_version_passes_through(
        self,
        mock_lookup_client,
        mock_slack_service,
        mock_posthog,
    ):
        # v3.1 iOS in the field today sends no fingerprint and no UA, but if
        # for some reason a v3.1 build carried a UA, it must still be lenient.
        app = _make_app(
            strict_flag=True,
            ban_check_client=None,
            lookup_client=mock_lookup_client,
            slack_service=mock_slack_service,
            posthog_client=mock_posthog,
        )

        with patch(
            "routers.request.parse_request",
            new_callable=AsyncMock,
            return_value=SAMPLE_PARSED,
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play la paradoja"},
                    headers={"User-Agent": "WXYC-iOS/3.1.0"},
                )

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_no_user_agent_passes_through(
        self,
        mock_lookup_client,
        mock_slack_service,
        mock_posthog,
    ):
        # No UA at all — gate cannot identify the client, falls through.
        app = _make_app(
            strict_flag=True,
            ban_check_client=None,
            lookup_client=mock_lookup_client,
            slack_service=mock_slack_service,
            posthog_client=mock_posthog,
        )

        with patch(
            "routers.request.parse_request",
            new_callable=AsyncMock,
            return_value=SAMPLE_PARSED,
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                # AsyncClient sets a default UA; explicitly drop it.
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play la paradoja"},
                    headers={"User-Agent": ""},
                )

        assert response.status_code == 200


class TestUAGateOff:
    """With the flag off, the gate is a no-op regardless of other state."""

    @pytest.mark.asyncio
    async def test_flag_off_lets_known_client_through_without_fingerprint(
        self,
        mock_lookup_client,
        mock_slack_service,
        mock_posthog,
    ):
        # Same shape as the blocked test, but strict_flag=False.
        app = _make_app(
            strict_flag=False,
            ban_check_client=None,
            lookup_client=mock_lookup_client,
            slack_service=mock_slack_service,
            posthog_client=mock_posthog,
        )

        with patch(
            "routers.request.parse_request",
            new_callable=AsyncMock,
            return_value=SAMPLE_PARSED,
        ):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/request",
                    json={"message": "play la paradoja"},
                    headers={"User-Agent": "WXYC-iOS/3.2.0"},
                )

        assert response.status_code == 200
        mock_posthog.capture.assert_called()
        # Verify no request_blocked event was emitted.
        for call in mock_posthog.capture.call_args_list:
            assert call.kwargs.get("event") != "request_blocked"
