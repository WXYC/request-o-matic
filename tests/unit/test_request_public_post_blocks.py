"""Unit tests pinning what ``POST /request`` puts in the **public** channel post.

Two properties, and the second is why this file still exists after the first
one inverted:

1. **No ban menu.** The "Ban requester" overflow used to ride on every public
   post (request-o-matic#152, moved into an overflow by #237). Slack has no
   per-viewer block visibility, so a menu on a channel post is a menu every DJ
   in ``#wxyc-requests`` can see and only four accounts can use -- which is
   what a DJ asked about on 2026-08-31. The affordance is being re-homed to a
   moderators-only channel; until then the public post carries none.

2. **The fingerprint metadata survives.** ``build_slack_metadata`` is a
   separate call from the menu, and deleting the visible affordance must not
   take the ban *target* with it. The re-homed surface reads the fingerprint
   out of exactly this envelope, so an edit that drops it would silently make
   the follow-up impossible to build -- and would do so without failing any
   test that only looked at blocks.

``services/slack.maybe_append_ban_button`` keeps its own unit tests in
``tests/unit/test_slack_service.py``; these cover the two call sites in
``routers/request.py`` (the clean/search-degraded path and the parsing-degraded
early return).
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
from services.slack import BAN_BUTTON_ACTION_ID, SLACK_METADATA_EVENT_TYPE
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


def _posted_metadata(mock_slack_service):
    return mock_slack_service.post_blocks.call_args.kwargs.get("metadata")


def _has_ban_menu(blocks) -> bool:
    return any(
        block.get("type") == "actions"
        and any(el.get("action_id") == BAN_BUTTON_ACTION_ID for el in block.get("elements", []))
        for block in blocks
    )


async def _run_clean_path(mock_slack_service, *, fingerprint: str | None):
    lookup_client = AsyncMock(spec=LookupServiceClient)
    lookup_client.lookup.return_value = LookupResult(
        response=LookupResponse(results=[], search_type=SearchType.none),
        server_timing=None,
    )
    app = _make_app(lookup_client=lookup_client, slack_service=mock_slack_service)
    parsed = make_parsed_request(song="la paradoja", artist="juana molina", raw_message=MESSAGE)

    with patch("routers.request.parse_request", new_callable=AsyncMock, return_value=parsed):
        await _post(app, fingerprint=fingerprint)


async def _run_parsing_degraded_path(mock_slack_service, *, fingerprint: str | None):
    lookup_client = AsyncMock(spec=LookupServiceClient)
    app = _make_app(lookup_client=lookup_client, slack_service=mock_slack_service)

    with patch(
        "routers.request.parse_request",
        new_callable=AsyncMock,
        side_effect=RuntimeError("groq down"),
    ):
        await _post(app, fingerprint=fingerprint)


PATHS = {
    "clean": _run_clean_path,
    "parsing_degraded": _run_parsing_degraded_path,
}


class TestPublicPostCarriesNoBanMenu:
    """The public channel post never renders the ban affordance, fingerprint or not.

    Parameterized over both call sites rather than duplicated per path: the two
    differ only in how they reach ``post_blocks``, and pinning them separately
    is what let one of them drift in the first place.
    """

    @pytest.mark.parametrize("path", PATHS.values(), ids=list(PATHS))
    @pytest.mark.parametrize("fingerprint", [FINGERPRINT, None], ids=["fingerprint", "anonymous"])
    @pytest.mark.asyncio
    async def test_no_ban_menu(self, mock_slack_service, path, fingerprint):
        await path(mock_slack_service, fingerprint=fingerprint)
        assert not _has_ban_menu(_posted_blocks(mock_slack_service))


class TestPublicPostStillCarriesFingerprintMetadata:
    """Removing the visible menu must not remove the ban target.

    The interactivity handler resolves a fingerprint from the clicked message's
    metadata, so this envelope is the sole carrier and the only thing a
    moderators-channel re-home needs from this router.
    """

    @pytest.mark.parametrize("path", PATHS.values(), ids=list(PATHS))
    @pytest.mark.asyncio
    async def test_metadata_present_with_fingerprint(self, mock_slack_service, path):
        await path(mock_slack_service, fingerprint=FINGERPRINT)

        metadata = _posted_metadata(mock_slack_service)
        assert metadata is not None
        assert metadata["event_type"] == SLACK_METADATA_EVENT_TYPE
        assert metadata["event_payload"]["fingerprint"] == FINGERPRINT

    @pytest.mark.parametrize("path", PATHS.values(), ids=list(PATHS))
    @pytest.mark.asyncio
    async def test_metadata_absent_without_fingerprint(self, mock_slack_service, path):
        await path(mock_slack_service, fingerprint=None)
        assert _posted_metadata(mock_slack_service) is None
