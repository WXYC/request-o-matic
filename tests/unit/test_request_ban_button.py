"""Unit tests pinning that ``POST /request`` attaches the "Ban requester"
button (request-o-matic#152) to the blocks it actually posts to Slack.

``services/slack.maybe_append_ban_button`` is unit-tested directly in
``tests/unit/test_slack_service.py``; these tests cover the two call sites in
``routers/request.py`` (the clean/search-degraded path and the
parsing-degraded early return) so a future edit to either site can't silently
stop wiring the fingerprint through.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from core.dependencies import (
    get_groq_client,
    get_lookup_client,
    get_posthog_client,
    get_slack_service,
)
from generated.api_models import SearchType
from routers.request import router
from services.lookup_client import LookupResponse, LookupResult, LookupServiceClient
from services.slack import BAN_BUTTON_ACTION_ID
from tests.conftest import make_parsed_request

MESSAGE = "play la paradoja by juana molina"
FINGERPRINT = "11111111-2222-3333-4444-555555555555"


def _make_app(*, lookup_client, slack_service, posthog_client=None):
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_groq_client] = lambda: Mock()
    app.dependency_overrides[get_slack_service] = lambda: slack_service
    app.dependency_overrides[get_posthog_client] = lambda: posthog_client or Mock()
    app.dependency_overrides[get_lookup_client] = lambda: lookup_client
    return app


@pytest.fixture
def mock_slack_service():
    svc = AsyncMock()
    svc.post_blocks = AsyncMock()
    svc.webhook_url = "https://hooks.slack.com/test"
    return svc


async def _post(app, *, fingerprint: str | None = None):
    headers = {} if fingerprint is None else {"X-Device-Fingerprint": fingerprint}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post("/api/v1/request", json={"message": MESSAGE}, headers=headers)


def _posted_blocks(mock_slack_service):
    _, kwargs = mock_slack_service.post_blocks.call_args
    args = mock_slack_service.post_blocks.call_args.args
    return args[0] if args else kwargs["blocks"]


def _has_ban_button(blocks) -> bool:
    return any(
        block.get("type") == "actions"
        and any(el.get("action_id") == BAN_BUTTON_ACTION_ID for el in block.get("elements", []))
        for block in blocks
    )


class TestCleanPathBanButton:
    @pytest.mark.asyncio
    async def test_button_present_with_fingerprint(self, mock_slack_service):
        lookup_client = AsyncMock(spec=LookupServiceClient)
        lookup_client.lookup.return_value = LookupResult(
            response=LookupResponse(results=[], search_type=SearchType.none),
            server_timing=None,
        )
        app = _make_app(lookup_client=lookup_client, slack_service=mock_slack_service)
        parsed = make_parsed_request(song="la paradoja", artist="juana molina", raw_message=MESSAGE)

        with patch("routers.request.parse_request", new_callable=AsyncMock, return_value=parsed):
            await _post(app, fingerprint=FINGERPRINT)

        assert _has_ban_button(_posted_blocks(mock_slack_service))

    @pytest.mark.asyncio
    async def test_no_button_without_fingerprint(self, mock_slack_service):
        lookup_client = AsyncMock(spec=LookupServiceClient)
        lookup_client.lookup.return_value = LookupResult(
            response=LookupResponse(results=[], search_type=SearchType.none),
            server_timing=None,
        )
        app = _make_app(lookup_client=lookup_client, slack_service=mock_slack_service)
        parsed = make_parsed_request(song="la paradoja", artist="juana molina", raw_message=MESSAGE)

        with patch("routers.request.parse_request", new_callable=AsyncMock, return_value=parsed):
            await _post(app, fingerprint=None)

        assert not _has_ban_button(_posted_blocks(mock_slack_service))


class TestParsingDegradedPathBanButton:
    @pytest.mark.asyncio
    async def test_button_present_with_fingerprint(self, mock_slack_service):
        lookup_client = AsyncMock(spec=LookupServiceClient)
        app = _make_app(lookup_client=lookup_client, slack_service=mock_slack_service)

        with patch(
            "routers.request.parse_request",
            new_callable=AsyncMock,
            side_effect=RuntimeError("groq down"),
        ):
            await _post(app, fingerprint=FINGERPRINT)

        assert _has_ban_button(_posted_blocks(mock_slack_service))

    @pytest.mark.asyncio
    async def test_no_button_without_fingerprint(self, mock_slack_service):
        lookup_client = AsyncMock(spec=LookupServiceClient)
        app = _make_app(lookup_client=lookup_client, slack_service=mock_slack_service)

        with patch(
            "routers.request.parse_request",
            new_callable=AsyncMock,
            side_effect=RuntimeError("groq down"),
        ):
            await _post(app, fingerprint=None)

        assert not _has_ban_button(_posted_blocks(mock_slack_service))
