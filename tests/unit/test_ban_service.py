"""Unit tests for services/ban_service.py — the shared mutation surface.

These tests pin the contract that both the HTTP admin router (#151) and the
Slack-native ban router (#152) rely on:

* ``ban(client, ...)`` forwards the call to BanAdminClient with the same shape
* ``unban(client, ...)`` forwards similarly
* ``actor`` translates to ``banned_by_user_id``
* ``list_bans(client, ...)`` is a pure pass-through
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from services import ban_service

SAMPLE_FP = "11111111-2222-3333-4444-555555555555"


def _client_mock() -> AsyncMock:
    """Build an AsyncMock that quacks like BanAdminClient."""
    client = AsyncMock()
    client.ban = AsyncMock(
        return_value={
            "fingerprint": SAMPLE_FP,
            "banned_at": "2026-01-01T00:00:00Z",
            "ban_reason": "spam",
            "ban_expires_at": None,
            "banned_by_user_id": None,
        }
    )
    client.unban = AsyncMock(return_value=None)
    client.list_bans = AsyncMock(return_value={"items": [], "nextCursor": None})
    return client


class TestBan:
    @pytest.mark.asyncio
    async def test_forwards_minimal_args(self):
        client = _client_mock()
        await ban_service.ban(client, fingerprint=SAMPLE_FP, reason="spam")
        client.ban.assert_awaited_once_with(
            fingerprint=SAMPLE_FP,
            reason="spam",
            expires_in_seconds=None,
            banned_by_user_id=None,
        )

    @pytest.mark.asyncio
    async def test_actor_translates_to_banned_by_user_id(self):
        """actor (rom vocab) -> banned_by_user_id (BS vocab) at the client boundary."""
        client = _client_mock()
        await ban_service.ban(
            client,
            fingerprint=SAMPLE_FP,
            reason="spam",
            actor="user-abc",
        )
        _, kwargs = client.ban.call_args
        assert kwargs["banned_by_user_id"] == "user-abc"

    @pytest.mark.asyncio
    async def test_forwards_expires_in_seconds(self):
        client = _client_mock()
        await ban_service.ban(
            client,
            fingerprint=SAMPLE_FP,
            reason="spam",
            expires_in_seconds=3600,
        )
        _, kwargs = client.ban.call_args
        assert kwargs["expires_in_seconds"] == 3600

    @pytest.mark.asyncio
    async def test_returns_client_row_verbatim(self):
        """ban_service is a passthrough — no transformation of the BS response."""
        client = _client_mock()
        result = await ban_service.ban(client, fingerprint=SAMPLE_FP, reason="spam")
        assert result == {
            "fingerprint": SAMPLE_FP,
            "banned_at": "2026-01-01T00:00:00Z",
            "ban_reason": "spam",
            "ban_expires_at": None,
            "banned_by_user_id": None,
        }


class TestUnban:
    @pytest.mark.asyncio
    async def test_forwards_fingerprint(self):
        client = _client_mock()
        await ban_service.unban(client, fingerprint=SAMPLE_FP)
        client.unban.assert_awaited_once_with(fingerprint=SAMPLE_FP)

    @pytest.mark.asyncio
    async def test_actor_is_ignored_by_client_but_accepted_by_service(self):
        """``actor`` is logged but never forwarded to the client (BS DELETE has no audit field)."""
        client = _client_mock()
        await ban_service.unban(client, fingerprint=SAMPLE_FP, actor="user-abc")
        _, kwargs = client.unban.call_args
        assert "actor" not in kwargs
        assert "banned_by_user_id" not in kwargs

    @pytest.mark.asyncio
    async def test_returns_none(self):
        """unban returns None on success; the assertion is "no exception raised"."""
        client = _client_mock()
        await ban_service.unban(client, fingerprint=SAMPLE_FP)


class TestListBans:
    @pytest.mark.asyncio
    async def test_passthrough(self):
        """list_bans is a pure pass-through; no transformation of args or response."""
        client = _client_mock()
        result = await ban_service.list_bans(client, limit=10, cursor="abc")
        client.list_bans.assert_awaited_once_with(limit=10, cursor="abc")
        assert result == {"items": [], "nextCursor": None}

    @pytest.mark.asyncio
    async def test_omits_unset_args(self):
        client = _client_mock()
        await ban_service.list_bans(client)
        client.list_bans.assert_awaited_once_with(limit=None, cursor=None)
