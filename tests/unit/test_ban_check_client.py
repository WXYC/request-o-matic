"""Unit tests for services/ban_check_client.py.

The ban-check client wraps BS `POST /auth/check-request-ban` (WXYC/Backend-Service#1261).
Responsibilities:

- Build the request from the two caller-supplied headers (``Authorization`` and/or
  ``X-Device-Fingerprint``) and POST to BS auth.
- Parse 200 responses into a typed ``BanCheckResult``.
- Treat 401 (invalid/expired JWT) and 404 (user not found) as
  ``banned=False`` — i.e. proceed-as-unauth. The caller must NOT be 401'd.
- Raise ``BanCheckUnavailableError`` for network errors, timeouts, and 5xx responses.
  The router fails open on this exception.
- Skip the call entirely when neither header is present (caller's responsibility
  to gate, but we still validate here so the client is safe to call directly).
"""

from __future__ import annotations

import json

import httpx
import pytest

from services.ban_check_client import (
    BanCheckClient,
    BanCheckResult,
    BanCheckUnavailableError,
)


def _make_client(handler, *, retry_delay: float = 0.0) -> BanCheckClient:
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    return BanCheckClient(
        "http://bs-auth:8082/auth/check-request-ban",
        http_client,
        retry_delay=retry_delay,
    )


class TestBanCheckClientHappyPath:
    """Successful BS responses parse into BanCheckResult."""

    @pytest.mark.asyncio
    async def test_unbanned_user_with_jwt(self):
        captured: dict = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["headers"] = dict(request.headers)
            captured["url"] = str(request.url)
            captured["method"] = request.method
            return httpx.Response(
                200,
                json={"userId": "user-abc", "fingerprint": None, "banned": False},
            )

        client = _make_client(handler)
        result = await client.check(authorization="Bearer eyJhbGc...", fingerprint=None)

        assert isinstance(result, BanCheckResult)
        assert result.banned is False
        assert result.user_id == "user-abc"
        assert result.fingerprint is None
        assert result.ban_reason is None
        assert result.ban_source is None

        assert captured["method"] == "POST"
        assert captured["url"] == "http://bs-auth:8082/auth/check-request-ban"
        assert captured["headers"]["authorization"] == "Bearer eyJhbGc..."
        assert "x-device-fingerprint" not in captured["headers"]

    @pytest.mark.asyncio
    async def test_banned_user(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "userId": "user-banned",
                    "fingerprint": "11111111-1111-4111-8111-111111111111",
                    "banned": True,
                    "banReason": "Repeated abuse",
                    "banSource": "user",
                },
            )

        client = _make_client(handler)
        result = await client.check(
            authorization="Bearer eyJ...",
            fingerprint="11111111-1111-4111-8111-111111111111",
        )

        assert result.banned is True
        assert result.user_id == "user-banned"
        assert result.fingerprint == "11111111-1111-4111-8111-111111111111"
        assert result.ban_reason == "Repeated abuse"
        assert result.ban_source == "user"

    @pytest.mark.asyncio
    async def test_banned_fingerprint_only(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "userId": None,
                    "fingerprint": "22222222-2222-4222-8222-222222222222",
                    "banned": True,
                    "banReason": "Spam",
                    "banSource": "fingerprint",
                },
            )

        client = _make_client(handler)
        result = await client.check(
            authorization=None,
            fingerprint="22222222-2222-4222-8222-222222222222",
        )

        assert result.banned is True
        assert result.user_id is None
        assert result.fingerprint == "22222222-2222-4222-8222-222222222222"
        assert result.ban_source == "fingerprint"

    @pytest.mark.asyncio
    async def test_forwards_both_headers(self):
        captured: dict = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["headers"] = dict(request.headers)
            return httpx.Response(200, json={"userId": "u", "fingerprint": "f", "banned": False})

        client = _make_client(handler)
        await client.check(
            authorization="Bearer abc",
            fingerprint="33333333-3333-4333-8333-333333333333",
        )

        assert captured["headers"]["authorization"] == "Bearer abc"
        assert captured["headers"]["x-device-fingerprint"] == "33333333-3333-4333-8333-333333333333"


class TestBanCheckClientProceedAsUnauth:
    """401 / 404 from BS → proceed-as-unauth (banned=False), NOT an error."""

    @pytest.mark.asyncio
    async def test_invalid_token_returns_unbanned(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "invalid_token"})

        client = _make_client(handler)
        result = await client.check(authorization="Bearer garbage", fingerprint=None)

        assert result.banned is False
        assert result.user_id is None
        assert result.fingerprint is None

    @pytest.mark.asyncio
    async def test_user_not_found_returns_unbanned(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"error": "user_not_found"})

        client = _make_client(handler)
        result = await client.check(authorization="Bearer ghost", fingerprint=None)

        assert result.banned is False
        assert result.user_id is None


class TestBanCheckClientFailOpen:
    """Network/timeout/5xx → BanCheckUnavailableError so the router can fail open."""

    @pytest.mark.parametrize(
        "exc_factory",
        [
            lambda: httpx.ConnectError("Connection refused"),
            lambda: httpx.TimeoutException("Read timed out"),
        ],
        ids=["connect_error", "timeout"],
    )
    @pytest.mark.asyncio
    async def test_network_errors_raise_unavailable(self, exc_factory):
        async def handler(request: httpx.Request) -> httpx.Response:
            raise exc_factory()

        client = _make_client(handler)
        with pytest.raises(BanCheckUnavailableError):
            await client.check(authorization="Bearer x", fingerprint=None)

    @pytest.mark.asyncio
    async def test_500_raises_unavailable(self):
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "internal_server_error"})

        client = _make_client(handler)
        with pytest.raises(BanCheckUnavailableError):
            await client.check(authorization="Bearer x", fingerprint=None)

    @pytest.mark.asyncio
    async def test_400_no_signal_raises_unavailable(self):
        """A 400 from BS indicates a contract bug on our end, not a banned user.

        Bubble it as unavailable so the router fails open and the operator sees
        the Sentry breadcrumb rather than silently banning every caller.
        """

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "no_signal"})

        client = _make_client(handler)
        with pytest.raises(BanCheckUnavailableError):
            await client.check(authorization="Bearer x", fingerprint=None)


class TestBanCheckClientRequestShape:
    """Request body and method invariants."""

    @pytest.mark.asyncio
    async def test_post_with_empty_body(self):
        """All signal travels in headers; the body is empty.

        BS#1261's handler reads ``Authorization`` and ``X-Device-Fingerprint``
        directly from the request headers — we don't send a JSON body.
        """
        captured: dict = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = request.content
            captured["method"] = request.method
            return httpx.Response(200, json={"userId": "u", "fingerprint": None, "banned": False})

        client = _make_client(handler)
        await client.check(authorization="Bearer x", fingerprint=None)

        assert captured["method"] == "POST"
        # Either empty or an empty JSON object — both are acceptable; nothing
        # load-bearing should travel in the body.
        if captured["body"]:
            assert json.loads(captured["body"]) == {}

    @pytest.mark.asyncio
    async def test_raises_when_no_signal_supplied(self):
        """Calling check() with neither header is a programmer error; raise."""

        async def handler(request: httpx.Request) -> httpx.Response:
            pytest.fail("BS must not be called when no signal is present")

        client = _make_client(handler)
        with pytest.raises(ValueError):
            await client.check(authorization=None, fingerprint=None)
