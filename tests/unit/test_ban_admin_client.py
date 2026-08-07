"""Unit tests for services/ban_admin_client.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from services.ban_admin_client import BanAdminClient, BanAdminClientError

BS_BASE = "https://bs.example.com/internal/banned-fingerprints"
SAMPLE_FP = "11111111-2222-3333-4444-555555555555"
INTERNAL_KEY = "test-internal-key"


def _client(http_client: object) -> BanAdminClient:
    """Helper: build a BanAdminClient against the supplied (Async)Mock."""
    return BanAdminClient(BS_BASE, http_client, internal_key=INTERNAL_KEY)  # type: ignore[arg-type]


def _ok_response(status_code: int, body: object | None = None) -> Mock:
    resp = Mock()
    resp.status_code = status_code
    if body is None:
        resp.json = Mock(side_effect=ValueError("no json"))
        resp.text = ""
    else:
        resp.json = Mock(return_value=body)
        resp.text = str(body)
    return resp


class TestBan:
    """Tests for BanAdminClient.ban (POST upsert)."""

    @pytest.mark.asyncio
    async def test_sends_x_internal_key_header(self):
        """Every POST carries X-Internal-Key sourced from settings."""
        http = AsyncMock()
        http.post = AsyncMock(
            return_value=_ok_response(
                200,
                {
                    "fingerprint": SAMPLE_FP,
                    "banned_at": "2026-01-01T00:00:00Z",
                    "ban_reason": "spam",
                    "ban_expires_at": None,
                    "banned_by_user_id": None,
                },
            )
        )

        await _client(http).ban(fingerprint=SAMPLE_FP, reason="spam")

        called_url, called_kwargs = http.post.call_args
        assert called_url[0] == BS_BASE
        assert called_kwargs["headers"] == {"X-Internal-Key": INTERNAL_KEY}

    @pytest.mark.asyncio
    async def test_serializes_camelcase_fields(self):
        """expires_in_seconds and banned_by_user_id translate to camelCase BS fields."""
        http = AsyncMock()
        http.post = AsyncMock(
            return_value=_ok_response(
                200,
                {
                    "fingerprint": SAMPLE_FP,
                    "banned_at": "2026-01-01T00:00:00Z",
                    "ban_reason": "spam",
                    "ban_expires_at": "2026-01-08T00:00:00Z",
                    "banned_by_user_id": "user-abc",
                },
            )
        )

        await _client(http).ban(
            fingerprint=SAMPLE_FP,
            reason="spam",
            expires_in_seconds=604800,
            banned_by_user_id="user-abc",
        )

        _, called_kwargs = http.post.call_args
        assert called_kwargs["json"] == {
            "fingerprint": SAMPLE_FP,
            "reason": "spam",
            "expiresInSeconds": 604800,
            "bannedByUserId": "user-abc",
        }

    @pytest.mark.asyncio
    async def test_omits_optional_fields_when_none(self):
        """None-valued optional fields are omitted from the payload entirely."""
        http = AsyncMock()
        http.post = AsyncMock(
            return_value=_ok_response(
                200,
                {
                    "fingerprint": SAMPLE_FP,
                    "banned_at": "2026-01-01T00:00:00Z",
                    "ban_reason": "spam",
                    "ban_expires_at": None,
                    "banned_by_user_id": None,
                },
            )
        )

        await _client(http).ban(fingerprint=SAMPLE_FP, reason="spam")

        _, called_kwargs = http.post.call_args
        assert called_kwargs["json"] == {"fingerprint": SAMPLE_FP, "reason": "spam"}

    @pytest.mark.asyncio
    async def test_returns_upserted_row(self):
        """Successful 200 returns the parsed JSON body verbatim."""
        body = {
            "fingerprint": SAMPLE_FP,
            "banned_at": "2026-01-01T00:00:00Z",
            "ban_reason": "spam",
            "ban_expires_at": None,
            "banned_by_user_id": None,
        }
        http = AsyncMock()
        http.post = AsyncMock(return_value=_ok_response(200, body))

        row = await _client(http).ban(fingerprint=SAMPLE_FP, reason="spam")

        assert row == body

    @pytest.mark.asyncio
    async def test_raises_on_400(self):
        """A 400 response (e.g. malformed fingerprint) becomes BanAdminClientError."""
        http = AsyncMock()
        http.post = AsyncMock(
            return_value=_ok_response(400, {"error": "fingerprint must be a valid UUID"})
        )

        with pytest.raises(BanAdminClientError) as excinfo:
            await _client(http).ban(fingerprint="not-a-uuid", reason="spam")

        assert excinfo.value.status_code == 400
        assert excinfo.value.body == {"error": "fingerprint must be a valid UUID"}

    @pytest.mark.asyncio
    async def test_raises_on_500(self):
        """5xx upstream errors are also wrapped (router decides 502 mapping)."""
        http = AsyncMock()
        http.post = AsyncMock(return_value=_ok_response(500, {"error": "Internal server error"}))

        with pytest.raises(BanAdminClientError) as excinfo:
            await _client(http).ban(fingerprint=SAMPLE_FP, reason="spam")

        assert excinfo.value.status_code == 500


class TestUnban:
    """Tests for BanAdminClient.unban (DELETE)."""

    @pytest.mark.asyncio
    async def test_targets_per_fingerprint_url(self):
        """DELETE goes to {base_url}/{fingerprint}, not the collection URL."""
        http = AsyncMock()
        http.delete = AsyncMock(return_value=_ok_response(204))

        await _client(http).unban(fingerprint=SAMPLE_FP)

        called_args, called_kwargs = http.delete.call_args
        assert called_args[0] == f"{BS_BASE}/{SAMPLE_FP}"
        assert called_kwargs["headers"] == {"X-Internal-Key": INTERNAL_KEY}

    @pytest.mark.asyncio
    async def test_returns_none_on_204(self):
        """204 (whether row existed or not) returns cleanly with no body."""
        http = AsyncMock()
        http.delete = AsyncMock(return_value=_ok_response(204))

        # unban() returns None on success; the assertion is "no exception raised".
        await _client(http).unban(fingerprint=SAMPLE_FP)

    @pytest.mark.asyncio
    async def test_raises_on_400(self):
        """Malformed fingerprint surfaces as BanAdminClientError(400)."""
        http = AsyncMock()
        http.delete = AsyncMock(
            return_value=_ok_response(400, {"error": "fingerprint must be a valid UUID"})
        )

        with pytest.raises(BanAdminClientError) as excinfo:
            await _client(http).unban(fingerprint="not-a-uuid")

        assert excinfo.value.status_code == 400


class TestListBans:
    """Tests for BanAdminClient.list_bans (GET)."""

    @pytest.mark.asyncio
    async def test_omits_query_params_when_unset(self):
        """No limit / cursor -> empty params dict (BS applies its own defaults)."""
        http = AsyncMock()
        http.get = AsyncMock(return_value=_ok_response(200, {"items": [], "nextCursor": None}))

        await _client(http).list_bans()

        _, called_kwargs = http.get.call_args
        assert called_kwargs["params"] == {}

    @pytest.mark.asyncio
    async def test_forwards_limit_and_cursor(self):
        """limit/cursor pass through as string query params."""
        http = AsyncMock()
        http.get = AsyncMock(return_value=_ok_response(200, {"items": [], "nextCursor": None}))

        await _client(http).list_bans(limit=25, cursor="2026-01-01T00:00:00.000Z|" + SAMPLE_FP)

        _, called_kwargs = http.get.call_args
        assert called_kwargs["params"] == {
            "limit": "25",
            "cursor": "2026-01-01T00:00:00.000Z|" + SAMPLE_FP,
        }

    @pytest.mark.asyncio
    async def test_returns_paginated_envelope(self):
        """Returns the {items, nextCursor} envelope verbatim."""
        body = {
            "items": [
                {
                    "fingerprint": SAMPLE_FP,
                    "banned_at": "2026-01-01T00:00:00Z",
                    "ban_reason": "spam",
                    "ban_expires_at": None,
                    "banned_by_user_id": None,
                }
            ],
            "nextCursor": None,
        }
        http = AsyncMock()
        http.get = AsyncMock(return_value=_ok_response(200, body))

        result = await _client(http).list_bans()

        assert result == body

    @pytest.mark.asyncio
    async def test_raises_on_non_200(self):
        """Any non-200 (e.g. bad limit) becomes BanAdminClientError."""
        http = AsyncMock()
        http.get = AsyncMock(return_value=_ok_response(400, {"error": "limit out of range"}))

        with pytest.raises(BanAdminClientError) as excinfo:
            await _client(http).list_bans(limit=999)

        assert excinfo.value.status_code == 400


class TestErrorBodyDecoding:
    """Tests for the JSON-or-text fallback in BanAdminClientError.body."""

    @pytest.mark.asyncio
    async def test_falls_back_to_text_when_body_not_json(self):
        """A non-JSON error body (e.g. an HTML 502 page) falls back to the raw text."""
        resp = Mock()
        resp.status_code = 502
        resp.json = Mock(side_effect=ValueError("not json"))
        resp.text = "<html>bad gateway</html>"
        http = AsyncMock()
        http.post = AsyncMock(return_value=resp)

        with pytest.raises(BanAdminClientError) as excinfo:
            await _client(http).ban(fingerprint=SAMPLE_FP, reason="spam")

        assert excinfo.value.status_code == 502
        assert excinfo.value.body == "<html>bad gateway</html>"


class TestRealHttpxResponse:
    """Sanity: BanAdminClient works against an actual httpx.Response (not a Mock).

    Catches accidental over-reliance on mock semantics — e.g. forgetting that
    httpx.Response.json() raises a JSONDecodeError subclass of ValueError.
    """

    @pytest.mark.asyncio
    async def test_post_with_real_response_envelope(self):
        body = {
            "fingerprint": SAMPLE_FP,
            "banned_at": "2026-01-01T00:00:00Z",
            "ban_reason": "spam",
            "ban_expires_at": None,
            "banned_by_user_id": None,
        }

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["x-internal-key"] == INTERNAL_KEY
            return httpx.Response(200, json=body)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http:
            result = await _client(http).ban(fingerprint=SAMPLE_FP, reason="spam")

        assert result == body


class TestTransportFailure:
    """Network/transport errors wrap as BanAdminClientError(status_code=0).

    Without this guard, httpx.HTTPError would escape the router's
    ``except BanAdminClientError`` and surface as an unhandled 500 — even
    though the docstring on _map_upstream_error and the route's responses=
    table both promise 502 for upstream failures.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc",
        [
            httpx.ConnectError("conn refused"),
            httpx.ReadTimeout("read timeout"),
            httpx.ConnectTimeout("connect timeout"),
            httpx.RemoteProtocolError("bad upstream"),
        ],
    )
    async def test_ban_transport_error_wraps_as_status_zero(self, exc):
        http = AsyncMock()
        http.post = AsyncMock(side_effect=exc)
        with pytest.raises(BanAdminClientError) as excinfo:
            await _client(http).ban(fingerprint=SAMPLE_FP, reason="spam")
        assert excinfo.value.status_code == 0
        assert excinfo.value.body["error"] == "upstream_unreachable"

    @pytest.mark.asyncio
    async def test_unban_transport_error_wraps_as_status_zero(self):
        http = AsyncMock()
        http.delete = AsyncMock(side_effect=httpx.ConnectError("conn refused"))
        with pytest.raises(BanAdminClientError) as excinfo:
            await _client(http).unban(fingerprint=SAMPLE_FP)
        assert excinfo.value.status_code == 0

    @pytest.mark.asyncio
    async def test_list_bans_transport_error_wraps_as_status_zero(self):
        http = AsyncMock()
        http.get = AsyncMock(side_effect=httpx.ReadTimeout("slow"))
        with pytest.raises(BanAdminClientError) as excinfo:
            await _client(http).list_bans()
        assert excinfo.value.status_code == 0


class TestNonJsonSuccess:
    """A 200 with non-JSON body (HTML from a reverse proxy, content-type drift,
    truncation) should NOT escape as an uncaught json.JSONDecodeError. Wrap
    it as BanAdminClientError so the router renders 502 via the existing
    upstream-error path.
    """

    @pytest.mark.asyncio
    async def test_ban_non_json_2xx_raises_ban_admin_client_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"<html>not json</html>")

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http:
            with pytest.raises(BanAdminClientError) as excinfo:
                await _client(http).ban(fingerprint=SAMPLE_FP, reason="spam")
        assert excinfo.value.status_code == 200
        assert excinfo.value.body["error"] == "non_json_upstream_body"

    @pytest.mark.asyncio
    async def test_list_bans_non_json_2xx_raises_ban_admin_client_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"<html>not json</html>")

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http:
            with pytest.raises(BanAdminClientError) as excinfo:
                await _client(http).list_bans()
        assert excinfo.value.status_code == 200


class TestUnbanUrlQuoting:
    """unban() must URL-quote the fingerprint so a non-FastAPI caller (e.g.
    the Slack-native router #152) can't inject path/query/fragment
    characters into the BS request URL.
    """

    @pytest.mark.asyncio
    async def test_unban_quotes_special_chars_in_path(self):
        seen_urls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_urls.append(str(request.url))
            return httpx.Response(204)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as http:
            # Caller-supplied value that would otherwise create a query string
            # rather than a path component.
            await _client(http).unban(fingerprint="aa?bb#cc")
        assert len(seen_urls) == 1
        # The '?' and '#' must be percent-encoded into the path; no query or
        # fragment may appear on the resulting URL.
        assert "?" not in seen_urls[0]
        assert "#" not in seen_urls[0]
        assert "aa%3Fbb%23cc" in seen_urls[0]
