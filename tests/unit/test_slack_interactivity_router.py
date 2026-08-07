"""Unit tests for routers/slack_interactivity.py -- POST /slack/interactivity
(request-o-matic#152).

Covers the full happy path against mocked Slack payloads plus the security
surface: signature verification (positive + negative) and allowlist
authorization, per the issue's explicit acceptance criteria. Mocking
strategy mirrors tests/unit/test_admin_router.py: the FastAPI dependencies
for SlackService and BanAdminClient are overridden with AsyncMocks so we
exercise the real router + ban_service code paths without a live Slack or
Backend-Service call.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock
from urllib.parse import quote

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from config.settings import Settings, get_settings
from core.dependencies import get_ban_admin_client
from core.exceptions import SlackPostError
from routers.slack_interactivity import (
    BAN_MODAL_CALLBACK_ID,
    BAN_REASON_ACTION_ID,
    BAN_REASON_BLOCK_ID,
    get_slack_interactivity_service,
    router,
)
from services.ban_admin_client import BanAdminClientError
from services.slack import BAN_BUTTON_ACTION_ID, SLACK_METADATA_EVENT_TYPE

SIGNING_SECRET = "test-signing-secret"
FINGERPRINT = "11111111-2222-3333-4444-555555555555"
CHANNEL_ID = "C0123456"
MESSAGE_TS = "1700000000.000100"
TRIGGER_ID = "trigger-abc123"
ACTING_USER = "U01ABC"


def _sign(secret: str, timestamp: str, body: bytes) -> str:
    basestring = f"v0:{timestamp}:".encode() + body
    digest = hmac.new(secret.encode("utf-8"), basestring, hashlib.sha256).hexdigest()
    return f"v0={digest}"


def _form_body(payload: dict) -> bytes:
    return f"payload={quote(json.dumps(payload))}".encode()


def _signed_headers(secret: str, body: bytes, *, timestamp: str | None = None) -> dict[str, str]:
    ts = timestamp or str(int(time.time()))
    return {
        "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": _sign(secret, ts, body),
        "Content-Type": "application/x-www-form-urlencoded",
    }


def _block_actions_payload(
    *,
    user_id: str = ACTING_USER,
    fingerprint: str | None = FINGERPRINT,
    action_id: str = BAN_BUTTON_ACTION_ID,
    blocks: list | None = None,
) -> dict:
    message: dict = {
        "ts": MESSAGE_TS,
        "blocks": blocks
        or [{"type": "section", "text": {"type": "mrkdwn", "text": "*Here's what I found:*"}}],
    }
    if fingerprint is not None:
        message["metadata"] = {
            "event_type": SLACK_METADATA_EVENT_TYPE,
            "event_payload": {"fingerprint": fingerprint},
        }
    return {
        "type": "block_actions",
        "trigger_id": TRIGGER_ID,
        "user": {"id": user_id},
        "channel": {"id": CHANNEL_ID},
        "message": message,
        "actions": [{"action_id": action_id, "type": "button"}],
    }


def _view_submission_payload(
    *,
    user_id: str = ACTING_USER,
    reason: str | None = "spamming the request line",
    fingerprint: str | None = FINGERPRINT,
    channel: str | None = CHANNEL_ID,
    message_ts: str | None = MESSAGE_TS,
    original_blocks: list | None = None,
    callback_id: str = BAN_MODAL_CALLBACK_ID,
    private_metadata: str | None = None,
) -> dict:
    if private_metadata is None:
        private_metadata = json.dumps(
            {
                "fingerprint": fingerprint,
                "channel": channel,
                "message_ts": message_ts,
                "blocks": original_blocks,
            }
        )
    values: dict = {}
    if reason is not None:
        values[BAN_REASON_BLOCK_ID] = {BAN_REASON_ACTION_ID: {"value": reason}}
    return {
        "type": "view_submission",
        "user": {"id": user_id},
        "view": {
            "callback_id": callback_id,
            "private_metadata": private_metadata,
            "state": {"values": values},
        },
    }


def _result_blocks(count: int) -> list[dict]:
    """Blocks shaped like a real multi-result request post.

    Nothing bounds the number of lookup results a post renders, and the
    Discogs artwork/permalink URLs each result carries are long, so a handful
    of them serializes past Slack's 3000-character private_metadata cap.
    """
    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": "*Here's what I found:*"}}
    ]
    catalog = [
        ("Juana Molina", "DOGA", "RO/MO/12"),
        ("Jessica Pratt", "On Your Own Love Again", "RO/PR/9"),
        ("Chuquimamani-Condori", "DJ E", "EL/CH/3"),
        ("Stereolab", "Aluminum Tunes", "RO/ST/44"),
        ("Sessa", "Pequena Vertigem de Amor", "RO/SE/7"),
        ("Cat Power", "Moon Pix", "RO/PO/21"),
    ]
    for index in range(count):
        artist, title, call_number = catalog[index % len(catalog)]
        release_id = 1234567 + index
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*{artist}*\n{title}\n_{call_number}_\n"
                        f"<https://www.discogs.com/release/{release_id}|Discogs> | "
                        f"<https://www.wxyc.info/playlists/album.jsp?id={release_id}|WXYC>"
                    ),
                },
                "accessory": {
                    "type": "image",
                    "image_url": (
                        f"https://i.discogs.com/{release_id}/rs:fit/g:sm/q:90/h:600/w:600/"
                        f"aHR0cHM6Ly9pLmRpc2NvZ3MuY29tL2FydHdvcmsvUi0{release_id}LmpwZWc.jpeg"
                    ),
                    "alt_text": f"{title} album cover",
                },
            }
        )
    return blocks


def _mock_slack_service() -> AsyncMock:
    service = AsyncMock()
    service.open_view = AsyncMock(return_value=None)
    service.update_message = AsyncMock(return_value=None)
    service.post_ephemeral = AsyncMock(return_value=None)
    return service


def _mock_ban_client() -> AsyncMock:
    client = AsyncMock()
    client.ban = AsyncMock(
        return_value={
            "fingerprint": FINGERPRINT,
            "banned_at": "2026-01-01T00:00:00Z",
            "ban_reason": "spamming the request line",
            "ban_expires_at": None,
            "banned_by_user_id": ACTING_USER,
        }
    )
    return client


def _build_app(
    *,
    signing_secret: str | None = SIGNING_SECRET,
    allowed_users: str | None = ACTING_USER,
    slack: AsyncMock | None = None,
    ban_client: AsyncMock | None = None,
    ban_upstream_configured: bool = True,
) -> FastAPI:
    app = FastAPI()
    app.include_router(router)

    settings = Settings(
        groq_api_key="test_groq_key",
        slack_signing_secret=signing_secret,
        slack_ban_authorized_users=allowed_users,
        bs_internal_bans_url=(
            "https://bs.example.com/internal/banned-fingerprints"
            if ban_upstream_configured
            else None
        ),
        bs_internal_key="test-internal-key" if ban_upstream_configured else None,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    if slack is not None:
        app.dependency_overrides[get_slack_interactivity_service] = lambda: slack
    if ban_client is not None:
        app.dependency_overrides[get_ban_admin_client] = lambda: ban_client

    return app


async def _post(app: FastAPI, body: bytes, headers: dict[str, str]):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.post("/slack/interactivity", content=body, headers=headers)


class TestSignatureVerification:
    @pytest.mark.asyncio
    async def test_valid_signature_is_accepted(self):
        payload = _block_actions_payload()
        body = _form_body(payload)
        app = _build_app(slack=_mock_slack_service(), ban_client=_mock_ban_client())

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_wrong_signature_rejected_401(self):
        payload = _block_actions_payload()
        body = _form_body(payload)
        app = _build_app(slack=_mock_slack_service(), ban_client=_mock_ban_client())

        headers = _signed_headers(SIGNING_SECRET, body)
        headers["X-Slack-Signature"] = "v0=" + "0" * 64

        resp = await _post(app, body, headers)

        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_missing_signature_headers_rejected_401(self):
        payload = _block_actions_payload()
        body = _form_body(payload)
        app = _build_app(slack=_mock_slack_service(), ban_client=_mock_ban_client())

        resp = await _post(app, body, {"Content-Type": "application/x-www-form-urlencoded"})

        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_no_signing_secret_configured_rejected_401(self):
        """Fail closed: unconfigured secret must reject everything, never skip
        verification."""
        payload = _block_actions_payload()
        body = _form_body(payload)
        app = _build_app(
            signing_secret=None, slack=_mock_slack_service(), ban_client=_mock_ban_client()
        )

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_non_ascii_signature_rejected_401_not_500(self):
        """Starlette decodes headers as latin-1, so a forged X-Slack-Signature
        can carry any byte >= 0x80. Comparing it as a str raised TypeError
        inside hmac.compare_digest, turning this unauthenticated, unrate-limited
        endpoint into a free 500 (and Sentry event) generator."""
        payload = _block_actions_payload()
        body = _form_body(payload)
        app = _build_app(slack=_mock_slack_service(), ban_client=_mock_ban_client())

        headers = [
            (b"content-type", b"application/x-www-form-urlencoded"),
            (b"x-slack-request-timestamp", str(int(time.time())).encode()),
            (b"x-slack-signature", b"v0=\xff" + b"0" * 63),
        ]
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.post("/slack/interactivity", content=body, headers=headers)

        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_stale_timestamp_rejected_401(self):
        payload = _block_actions_payload()
        body = _form_body(payload)
        app = _build_app(slack=_mock_slack_service(), ban_client=_mock_ban_client())

        stale_ts = str(int(time.time()) - 3600)
        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body, timestamp=stale_ts))

        assert resp.status_code == 401


class TestSignatureIsCheckedBeforeAnythingElse:
    """Signature verification must be the first thing that can affect the
    response.

    ``get_ban_admin_client`` 503s with the names of two env vars when
    BS_INTERNAL_BANS_URL/BS_INTERNAL_KEY are unset. Resolved as a handler
    parameter it ran *before* the in-body signature check, so an unsigned POST
    could read a deployment's configuration state straight off the response.
    """

    @pytest.mark.asyncio
    async def test_unsigned_request_to_unconfigured_deployment_is_401_not_503(self):
        payload = _block_actions_payload()
        body = _form_body(payload)
        app = _build_app(slack=_mock_slack_service(), ban_upstream_configured=False)

        resp = await _post(app, body, {"Content-Type": "application/x-www-form-urlencoded"})

        assert resp.status_code == 401
        assert "BS_INTERNAL" not in resp.text

    @pytest.mark.asyncio
    async def test_unsigned_request_looks_identical_configured_or_not(self):
        """An unauthenticated caller must not be able to tell a configured
        deployment from an unconfigured one."""
        payload = _block_actions_payload()
        body = _form_body(payload)
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        configured = await _post(
            _build_app(slack=_mock_slack_service(), ban_client=_mock_ban_client()), body, headers
        )
        unconfigured = await _post(
            _build_app(slack=_mock_slack_service(), ban_upstream_configured=False), body, headers
        )

        assert (configured.status_code, configured.text) == (
            unconfigured.status_code,
            unconfigured.text,
        )


class TestBlockActionsOpensModal:
    @pytest.mark.asyncio
    async def test_ban_button_click_opens_view(self):
        slack = _mock_slack_service()
        payload = _block_actions_payload()
        body = _form_body(payload)
        app = _build_app(slack=slack, ban_client=_mock_ban_client())

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.status_code == 200
        slack.open_view.assert_awaited_once()
        _, kwargs = slack.open_view.call_args
        assert kwargs["trigger_id"] == TRIGGER_ID
        view = kwargs["view"]
        assert view["callback_id"] == BAN_MODAL_CALLBACK_ID
        metadata = json.loads(view["private_metadata"])
        assert metadata["fingerprint"] == FINGERPRINT
        assert metadata["channel"] == CHANNEL_ID
        assert metadata["message_ts"] == MESSAGE_TS

    @pytest.mark.asyncio
    async def test_non_ban_action_id_ignored(self):
        slack = _mock_slack_service()
        payload = _block_actions_payload(action_id="some_other_button")
        body = _form_body(payload)
        app = _build_app(slack=slack, ban_client=_mock_ban_client())

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.status_code == 200
        slack.open_view.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_fingerprint_metadata_does_not_open_modal(self):
        """Defensive: the button is only ever rendered when a fingerprint is
        present, but a click on a message somehow missing it must not crash
        or open a modal with nothing to act on."""
        slack = _mock_slack_service()
        payload = _block_actions_payload(fingerprint=None)
        body = _form_body(payload)
        app = _build_app(slack=slack, ban_client=_mock_ban_client())

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.status_code == 200
        slack.open_view.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_fingerprint_notifies_clicker_instead_of_silent_noop(self):
        """A dead button (message posted via the webhook transport, which
        drops chat.postMessage metadata entirely) must tell the clicker
        something, not silently do nothing -- indistinguishable from an
        outage mid-incident otherwise."""
        slack = _mock_slack_service()
        payload = _block_actions_payload(fingerprint=None)
        body = _form_body(payload)
        app = _build_app(slack=slack, ban_client=_mock_ban_client())

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.status_code == 200
        slack.post_ephemeral.assert_awaited_once()
        _, kwargs = slack.post_ephemeral.call_args
        assert kwargs["channel"] == CHANNEL_ID
        assert kwargs["user"] == ACTING_USER


class TestViewSubmissionHappyPath:
    @pytest.mark.asyncio
    async def test_authorized_submit_bans_acks_and_edits_message(self):
        slack = _mock_slack_service()
        ban_client = _mock_ban_client()
        payload = _view_submission_payload(original_blocks=_result_blocks(1))
        body = _form_body(payload)
        app = _build_app(slack=slack, ban_client=ban_client)

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.status_code == 200
        ban_client.ban.assert_awaited_once()
        _, kwargs = ban_client.ban.call_args
        assert kwargs["fingerprint"] == FINGERPRINT
        assert kwargs["reason"] == "spamming the request line"
        # actor=None, not the Slack user ID: BS's banned_by_user_id
        # references better-auth's user.id, which a Slack user has no row
        # in -- see services/ban_service.py's module docstring. The acting
        # user is still recorded in the ephemeral ack + edited footer below.
        assert kwargs["banned_by_user_id"] is None

        slack.post_ephemeral.assert_awaited_once()
        _, ack_kwargs = slack.post_ephemeral.call_args
        assert ack_kwargs["channel"] == CHANNEL_ID
        assert ack_kwargs["user"] == ACTING_USER

        slack.update_message.assert_awaited_once()
        _, update_kwargs = slack.update_message.call_args
        assert update_kwargs["channel"] == CHANNEL_ID
        assert update_kwargs["ts"] == MESSAGE_TS
        rendered = json.dumps(update_kwargs["blocks"])
        assert ACTING_USER in rendered
        assert "spamming the request line" in rendered

    @pytest.mark.asyncio
    async def test_edit_preserves_original_blocks_and_appends_footer(self):
        slack = _mock_slack_service()
        ban_client = _mock_ban_client()
        original_blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": "*Here's what I found:*"}}
        ]
        payload = _view_submission_payload(original_blocks=original_blocks)
        body = _form_body(payload)
        app = _build_app(slack=slack, ban_client=ban_client)

        await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        _, update_kwargs = slack.update_message.call_args
        blocks = update_kwargs["blocks"]
        assert blocks[0] == original_blocks[0]
        assert len(blocks) == len(original_blocks) + 1


class TestOversizedMessageSurvivesTheBan:
    """A post too long to stash must be left alone, not overwritten.

    ``chat.update`` replaces a message's blocks wholesale. Nulling the stashed
    blocks on private_metadata overflow and editing anyway replaced the whole
    request post -- the thing the channel was actually looking at -- with a
    lone ban footer. Nothing bounds the number of lookup results a post
    renders, so this is an ordinary post, not an edge case.
    """

    @pytest.mark.asyncio
    async def test_long_post_overflows_private_metadata(self):
        """Guard the premise: these blocks really do exceed Slack's cap."""
        slack = _mock_slack_service()
        blocks = _result_blocks(8)
        assert len(json.dumps(blocks)) > 3000

        payload = _block_actions_payload(blocks=blocks)
        body = _form_body(payload)
        app = _build_app(slack=slack, ban_client=_mock_ban_client())

        await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        _, kwargs = slack.open_view.call_args
        private_metadata = kwargs["view"]["private_metadata"]
        assert len(private_metadata) <= 3000
        assert json.loads(private_metadata)["blocks"] is None

    @pytest.mark.asyncio
    async def test_ban_on_long_post_leaves_the_original_content_intact(self):
        """Chains the real block_actions private_metadata into the matching
        view_submission, so the two halves can't drift apart."""
        slack = _mock_slack_service()
        ban_client = _mock_ban_client()
        app = _build_app(slack=slack, ban_client=ban_client)

        click = _form_body(_block_actions_payload(blocks=_result_blocks(8)))
        await _post(app, click, _signed_headers(SIGNING_SECRET, click))
        private_metadata = slack.open_view.call_args[1]["view"]["private_metadata"]

        submit = _form_body(_view_submission_payload(private_metadata=private_metadata))
        resp = await _post(app, submit, _signed_headers(SIGNING_SECRET, submit))

        assert resp.status_code == 200
        # The ban itself still lands -- only the cosmetic footer is given up.
        ban_client.ban.assert_awaited_once()
        slack.update_message.assert_not_awaited()
        # ...and the operator is told, rather than left believing the post was
        # annotated.
        ack = slack.post_ephemeral.call_args[1]["text"]
        assert "Banned." in ack
        assert "too long to annotate" in ack


class TestViewSubmissionAuthorization:
    @pytest.mark.asyncio
    async def test_unauthorized_user_gets_ephemeral_refusal_and_no_ban(self):
        slack = _mock_slack_service()
        ban_client = _mock_ban_client()
        payload = _view_submission_payload(user_id="U99UNAUTHORIZED")
        body = _form_body(payload)
        app = _build_app(slack=slack, ban_client=ban_client, allowed_users=ACTING_USER)

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.status_code == 200
        ban_client.ban.assert_not_awaited()
        slack.post_ephemeral.assert_awaited_once()
        slack.update_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_allowlist_denies_everyone(self):
        """SLACK_BAN_AUTHORIZED_USERS unset/empty -> deny-all, even the user
        who would otherwise be allowlisted -- fail closed."""
        slack = _mock_slack_service()
        ban_client = _mock_ban_client()
        payload = _view_submission_payload(user_id=ACTING_USER)
        body = _form_body(payload)
        app = _build_app(slack=slack, ban_client=ban_client, allowed_users=None)

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.status_code == 200
        ban_client.ban.assert_not_awaited()
        slack.post_ephemeral.assert_awaited_once()


class TestViewSubmissionValidation:
    @pytest.mark.asyncio
    async def test_reason_too_long_returns_validation_error_not_502(self):
        slack = _mock_slack_service()
        ban_client = _mock_ban_client()
        payload = _view_submission_payload(reason="x" * 1001)
        body = _form_body(payload)
        app = _build_app(slack=slack, ban_client=ban_client)

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.status_code == 200
        data = resp.json()
        assert data["response_action"] == "errors"
        assert BAN_REASON_BLOCK_ID in data["errors"]
        ban_client.ban.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_reason_returns_validation_error(self):
        slack = _mock_slack_service()
        ban_client = _mock_ban_client()
        payload = _view_submission_payload(reason=None)
        body = _form_body(payload)
        app = _build_app(slack=slack, ban_client=ban_client)

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.status_code == 200
        data = resp.json()
        assert data["response_action"] == "errors"
        ban_client.ban.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ignores_view_submission_for_other_modal(self):
        """A callback_id that isn't ours (some future modal) must be a no-op,
        not an error."""
        slack = _mock_slack_service()
        ban_client = _mock_ban_client()
        payload = _view_submission_payload(callback_id="some_other_modal")
        body = _form_body(payload)
        app = _build_app(slack=slack, ban_client=ban_client)

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.status_code == 200
        ban_client.ban.assert_not_awaited()


class TestBanUpstreamFailure:
    @pytest.mark.asyncio
    async def test_ban_admin_client_error_acks_failure_not_502(self):
        """Backend-Service rejecting the ban must not surface as a raw 500 to
        Slack (Slack retries on 5xx, which would double-ban-attempt); tell the
        clicking user via ephemeral instead."""
        slack = _mock_slack_service()
        ban_client = _mock_ban_client()
        ban_client.ban = AsyncMock(side_effect=BanAdminClientError(502, {"error": "upstream_down"}))
        payload = _view_submission_payload()
        body = _form_body(payload)
        app = _build_app(slack=slack, ban_client=ban_client)

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.status_code == 200
        slack.post_ephemeral.assert_awaited_once()
        slack.update_message.assert_not_awaited()


class TestSlackUnavailable:
    @pytest.mark.asyncio
    async def test_block_actions_without_slack_service_does_not_crash(self):
        """SLACK_BOT_TOKEN unset (or Slack integration off) -> no interactivity
        service; the router must degrade gracefully rather than 500."""
        payload = _block_actions_payload()
        body = _form_body(payload)
        app = _build_app(slack=None, ban_client=_mock_ban_client())
        app.dependency_overrides[get_slack_interactivity_service] = lambda: None

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_open_view_failure_does_not_crash(self):
        slack = _mock_slack_service()
        slack.open_view = AsyncMock(side_effect=SlackPostError("expired_trigger_id"))
        payload = _block_actions_payload()
        body = _form_body(payload)
        app = _build_app(slack=slack, ban_client=_mock_ban_client())

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_post_ephemeral_failure_after_successful_ban_does_not_500(self):
        """The ban already landed in BS by the time the ack is sent -- a
        failed chat.postEphemeral (e.g. channel_not_found) must not surface
        as a 500, or Slack could retry the view_submission for an action that
        already succeeded."""
        slack = _mock_slack_service()
        slack.post_ephemeral = AsyncMock(side_effect=SlackPostError("channel_not_found"))
        ban_client = _mock_ban_client()
        payload = _view_submission_payload()
        body = _form_body(payload)
        app = _build_app(slack=slack, ban_client=ban_client)

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.status_code == 200
        ban_client.ban.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_update_message_httpx_error_after_successful_ban_does_not_500(self):
        """A raw httpx.HTTPError (e.g. raise_for_status on a 5xx) must be
        swallowed exactly like SlackPostError -- both are just "the
        best-effort notification failed", not "the request failed"."""
        slack = _mock_slack_service()
        slack.update_message = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "Error",
                request=httpx.Request("POST", "https://slack.com"),
                response=httpx.Response(500),
            )
        )
        ban_client = _mock_ban_client()
        payload = _view_submission_payload(original_blocks=_result_blocks(1))
        body = _form_body(payload)
        app = _build_app(slack=slack, ban_client=ban_client)

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.status_code == 200
        ban_client.ban.assert_awaited_once()
        slack.update_message.assert_awaited_once()
        slack.post_ephemeral.assert_awaited_once()
