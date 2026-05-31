"""Unit tests for routers/admin.py — the /admin/bans HTTP API (#151).

These tests cover:

* Bearer-token auth (mirror of LML's ``_validate_auth`` pattern)
* The three endpoints' happy paths
* Upstream-error translation (4xx forwarded, 5xx -> 502)
* The 503 fail-closed path when BS upstream config is missing

Mocking strategy: the FastAPI dependency for :class:`BanAdminClient` is
overridden with an :class:`unittest.mock.AsyncMock`, so we exercise the actual
ban_service + router code paths but never make a real HTTP call.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from config.settings import Settings, get_settings
from core.dependencies import get_ban_admin_client
from routers.admin import router
from services.ban_admin_client import BanAdminClientError

ADMIN_TOKEN = "test-admin-secret"
SAMPLE_FP = "11111111-2222-3333-4444-555555555555"
AUTH_HEADERS = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


def _build_app(
    *, admin_token: str | None = ADMIN_TOKEN, client: AsyncMock | None = None
) -> FastAPI:
    """Construct a fresh FastAPI app with the admin router and overridable deps."""
    app = FastAPI()
    app.include_router(router)

    test_settings = Settings(
        groq_api_key="test_groq_key",
        admin_token=admin_token,
        bs_internal_bans_url="https://bs.example.com/internal/banned-fingerprints",
        bs_internal_key="test-internal-key",
    )
    app.dependency_overrides[get_settings] = lambda: test_settings

    if client is not None:
        app.dependency_overrides[get_ban_admin_client] = lambda: client

    return app


def _ban_row(**overrides: object) -> dict:
    row: dict = {
        "fingerprint": SAMPLE_FP,
        "banned_at": "2026-01-01T00:00:00Z",
        "ban_reason": "spam",
        "ban_expires_at": None,
        "banned_by_user_id": None,
    }
    row.update(overrides)
    return row


def _mock_client() -> AsyncMock:
    client = AsyncMock()
    client.ban = AsyncMock(return_value=_ban_row())
    client.unban = AsyncMock(return_value=None)
    client.list_bans = AsyncMock(return_value={"items": [_ban_row()], "nextCursor": None})
    return client


# ---------------------------------------------------------------------------
# Auth tests — mirror LML admin pattern. Apply uniformly to all three routes.
# ---------------------------------------------------------------------------


class TestAuth:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("method", "url", "json_body"),
        [
            ("POST", "/admin/bans", {"fingerprint": SAMPLE_FP, "reason": "spam"}),
            ("DELETE", f"/admin/bans/{SAMPLE_FP}", None),
            ("GET", "/admin/bans", None),
        ],
    )
    async def test_missing_authorization_header_401(self, method, url, json_body):
        app = _build_app(client=_mock_client())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.request(method, url, json=json_body)
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Missing authorization"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("method", "url", "json_body"),
        [
            ("POST", "/admin/bans", {"fingerprint": SAMPLE_FP, "reason": "spam"}),
            ("DELETE", f"/admin/bans/{SAMPLE_FP}", None),
            ("GET", "/admin/bans", None),
        ],
    )
    async def test_wrong_bearer_token_403(self, method, url, json_body):
        app = _build_app(client=_mock_client())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.request(
                method, url, headers={"Authorization": "Bearer wrong-token"}, json=json_body
            )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Invalid token"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("method", "url", "json_body"),
        [
            ("POST", "/admin/bans", {"fingerprint": SAMPLE_FP, "reason": "spam"}),
            ("DELETE", f"/admin/bans/{SAMPLE_FP}", None),
            ("GET", "/admin/bans", None),
        ],
    )
    async def test_malformed_scheme_403(self, method, url, json_body):
        """``Authorization: <token>`` without the ``Bearer`` scheme -> 403."""
        app = _build_app(client=_mock_client())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.request(
                method, url, headers={"Authorization": ADMIN_TOKEN}, json=json_body
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_admin_token_unset_returns_403_fail_closed(self):
        """No ``ADMIN_TOKEN`` configured -> reject everything with 403 (fail-closed)."""
        app = _build_app(admin_token=None, client=_mock_client())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/admin/bans",
                headers={"Authorization": "Bearer anything"},
                json={"fingerprint": SAMPLE_FP, "reason": "spam"},
            )
        assert resp.status_code == 403
        assert "ADMIN_TOKEN" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_case_insensitive_bearer_scheme(self):
        """RFC 7235 allows case-insensitive scheme — accept ``bearer`` and ``BEARER``."""
        app = _build_app(client=_mock_client())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/admin/bans",
                headers={"Authorization": f"bearer {ADMIN_TOKEN}"},
                json={"fingerprint": SAMPLE_FP, "reason": "spam"},
            )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# POST /admin/bans
# ---------------------------------------------------------------------------


class TestCreateBan:
    @pytest.mark.asyncio
    async def test_minimal_ban_returns_200_with_row(self):
        client = _mock_client()
        app = _build_app(client=client)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/admin/bans",
                headers=AUTH_HEADERS,
                json={"fingerprint": SAMPLE_FP, "reason": "spam"},
            )

        assert resp.status_code == 200
        assert resp.json() == _ban_row()
        client.ban.assert_awaited_once_with(
            fingerprint=SAMPLE_FP,
            reason="spam",
            expires_in_seconds=None,
            banned_by_user_id=None,
        )

    @pytest.mark.asyncio
    async def test_passes_expires_in_seconds_through(self):
        client = _mock_client()
        app = _build_app(client=client)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/admin/bans",
                headers=AUTH_HEADERS,
                json={
                    "fingerprint": SAMPLE_FP,
                    "reason": "spam",
                    "expires_in_seconds": 3600,
                },
            )

        assert resp.status_code == 200
        _, kwargs = client.ban.call_args
        assert kwargs["expires_in_seconds"] == 3600

    @pytest.mark.asyncio
    async def test_http_admin_caller_has_no_actor(self):
        """HTTP-admin callers identified only by ADMIN_TOKEN forward NULL banned_by_user_id."""
        client = _mock_client()
        app = _build_app(client=client)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            await c.post(
                "/admin/bans",
                headers=AUTH_HEADERS,
                json={"fingerprint": SAMPLE_FP, "reason": "spam"},
            )

        _, kwargs = client.ban.call_args
        assert kwargs["banned_by_user_id"] is None

    @pytest.mark.asyncio
    async def test_re_ban_returns_200_with_updated_row_idempotent(self):
        """Re-banning an already-banned fingerprint returns 200 (idempotent)."""
        client = _mock_client()
        # BS upserts and returns the new banned_at / new reason
        client.ban = AsyncMock(
            return_value=_ban_row(banned_at="2026-02-01T00:00:00Z", ban_reason="still spam")
        )
        app = _build_app(client=client)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/admin/bans",
                headers=AUTH_HEADERS,
                json={"fingerprint": SAMPLE_FP, "reason": "still spam"},
            )

        assert resp.status_code == 200
        assert resp.json()["ban_reason"] == "still spam"
        assert resp.json()["banned_at"] == "2026-02-01T00:00:00Z"

    @pytest.mark.asyncio
    async def test_upstream_400_forwarded_as_400(self):
        """A BS 400 (malformed fingerprint) surfaces as 400 to the operator."""
        client = _mock_client()
        client.ban = AsyncMock(
            side_effect=BanAdminClientError(400, {"error": "fingerprint must be a valid UUID"})
        )
        app = _build_app(client=client)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/admin/bans",
                headers=AUTH_HEADERS,
                json={"fingerprint": SAMPLE_FP, "reason": "spam"},
            )

        assert resp.status_code == 400
        assert resp.json()["detail"]["upstream_status"] == 400
        assert resp.json()["detail"]["upstream_body"] == {
            "error": "fingerprint must be a valid UUID"
        }

    @pytest.mark.asyncio
    async def test_upstream_500_becomes_502(self):
        """An upstream 5xx surfaces as 502 (upstream broke, this service is fine)."""
        client = _mock_client()
        client.ban = AsyncMock(
            side_effect=BanAdminClientError(500, {"error": "Internal server error"})
        )
        app = _build_app(client=client)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/admin/bans",
                headers=AUTH_HEADERS,
                json={"fingerprint": SAMPLE_FP, "reason": "spam"},
            )

        assert resp.status_code == 502
        assert resp.json()["detail"]["upstream_status"] == 500


# ---------------------------------------------------------------------------
# DELETE /admin/bans/{fingerprint}
# ---------------------------------------------------------------------------


class TestDeleteBan:
    @pytest.mark.asyncio
    async def test_returns_204_on_success(self):
        client = _mock_client()
        app = _build_app(client=client)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.delete(f"/admin/bans/{SAMPLE_FP}", headers=AUTH_HEADERS)

        assert resp.status_code == 204
        assert resp.content == b""
        client.unban.assert_awaited_once_with(fingerprint=SAMPLE_FP)

    @pytest.mark.asyncio
    async def test_returns_204_idempotent_when_row_missing(self):
        """BS returns 204 whether or not the row existed; rom does the same."""
        client = _mock_client()
        client.unban = AsyncMock(return_value=None)  # BS 204 path
        app = _build_app(client=client)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.delete(f"/admin/bans/{SAMPLE_FP}", headers=AUTH_HEADERS)

        assert resp.status_code == 204

    @pytest.mark.asyncio
    async def test_upstream_400_forwarded(self):
        client = _mock_client()
        client.unban = AsyncMock(
            side_effect=BanAdminClientError(400, {"error": "fingerprint must be a valid UUID"})
        )
        app = _build_app(client=client)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.delete("/admin/bans/not-a-uuid", headers=AUTH_HEADERS)

        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# GET /admin/bans
# ---------------------------------------------------------------------------


class TestListBans:
    @pytest.mark.asyncio
    async def test_returns_paginated_envelope(self):
        client = _mock_client()
        app = _build_app(client=client)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/admin/bans", headers=AUTH_HEADERS)

        assert resp.status_code == 200
        assert resp.json() == {"items": [_ban_row()], "nextCursor": None}
        client.list_bans.assert_awaited_once_with(limit=None, cursor=None)

    @pytest.mark.asyncio
    async def test_forwards_limit_and_cursor(self):
        client = _mock_client()
        app = _build_app(client=client)

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get(
                "/admin/bans",
                headers=AUTH_HEADERS,
                params={"limit": "25", "cursor": "abc"},
            )

        assert resp.status_code == 200
        client.list_bans.assert_awaited_once_with(limit=25, cursor="abc")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_limit", ["0", "-1", "201", "abc"])
    async def test_rejects_out_of_range_limit_with_422(self, bad_limit):
        """FastAPI's Query(ge=1, le=200) returns 422 before hitting BS."""
        app = _build_app(client=_mock_client())
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/admin/bans", headers=AUTH_HEADERS, params={"limit": bad_limit})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Misconfiguration — BS upstream not wired
# ---------------------------------------------------------------------------


class TestUpstreamMisconfig:
    @pytest.mark.asyncio
    async def test_missing_bs_internal_bans_url_returns_503(self):
        """No ``BS_INTERNAL_BANS_URL`` -> 503 with operator-friendly message."""
        app = FastAPI()
        app.include_router(router)
        test_settings = Settings(
            groq_api_key="test_groq_key",
            admin_token=ADMIN_TOKEN,
            bs_internal_bans_url=None,
            bs_internal_key="test-internal-key",
        )
        app.dependency_overrides[get_settings] = lambda: test_settings

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post(
                "/admin/bans",
                headers=AUTH_HEADERS,
                json={"fingerprint": SAMPLE_FP, "reason": "spam"},
            )

        assert resp.status_code == 503
        assert "BS_INTERNAL_BANS_URL" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_missing_bs_internal_key_returns_503(self):
        """No ``BS_INTERNAL_KEY`` -> 503 as well (both halves required)."""
        app = FastAPI()
        app.include_router(router)
        test_settings = Settings(
            groq_api_key="test_groq_key",
            admin_token=ADMIN_TOKEN,
            bs_internal_bans_url="https://bs.example.com/internal/banned-fingerprints",
            bs_internal_key=None,
        )
        app.dependency_overrides[get_settings] = lambda: test_settings

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/admin/bans", headers=AUTH_HEADERS)

        assert resp.status_code == 503
