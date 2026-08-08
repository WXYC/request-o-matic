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
from urllib.parse import quote, urlencode

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from config.settings import Settings, get_settings
from core.dependencies import (
    BAN_ADMIN_UNCONFIGURED_DETAIL,
    get_moderator_client,
    get_optional_ban_admin_client,
)
from core.exceptions import SlackPostError
from routers.slack_interactivity import (
    BAN_MODAL_CALLBACK_ID,
    BAN_REASON_ACTION_ID,
    BAN_REASON_BLOCK_ID,
    get_slack_interactivity_service,
    router,
)
from services.ban_admin_client import BanAdminClientError
from services.moderator_client import ModeratorClientError
from services.slack import (
    BAN_BUTTON_ACTION_ID,
    BAN_MENU_OPTION_VALUE,
    SLACK_METADATA_EVENT_TYPE,
)

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
        # The realistic shape Slack sends for an overflow menu: ``action_id``
        # rides at the same level a button's does (which is why the handler's
        # matching needed no change), but the element type differs and a
        # ``selected_option`` comes along. The handler must keep ignoring that
        # option -- the fingerprint comes from verified message metadata.
        "actions": [
            {
                "action_id": action_id,
                "type": "overflow",
                "selected_option": {
                    "text": {"type": "plain_text", "text": "Ban requester"},
                    "value": BAN_MENU_OPTION_VALUE,
                },
            }
        ],
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
    moderator_client: AsyncMock | None = None,
    moderator_upstream_configured: bool = True,
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
        bs_internal_moderators_url=(
            "https://bs.example.com/internal/slack-ban-moderators"
            if moderator_upstream_configured
            else None
        ),
        bs_internal_key="test-internal-key" if ban_upstream_configured else None,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    if slack is not None:
        app.dependency_overrides[get_slack_interactivity_service] = lambda: slack
    # Overrides the OPTIONAL provider, which is what the route depends on now --
    # the raising variant would resolve before the interaction-type dispatch and
    # 503 every callback sharing this Request URL. dependency_overrides is
    # identity-keyed, so overriding the wrong one here silently does nothing and
    # the tests hit a real client against a fake URL.
    if ban_client is not None:
        app.dependency_overrides[get_optional_ban_admin_client] = lambda: ban_client
    # Default to no roster client so the pre-existing ban tests keep authorizing
    # off `allowed_users` alone, exactly as they did before the union existed.
    app.dependency_overrides[get_moderator_client] = lambda: moderator_client

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

    Historically ``get_ban_admin_client`` supplied the hazard: it 503s with the
    names of two env vars, and resolved as a handler parameter it ran *before*
    the in-body signature check, so an unsigned POST could read a deployment's
    configuration state straight off the response. This route no longer
    declares that provider, so the first test below can no longer fail for the
    right reason on its own -- see the injected-dependency test, which supplies
    the hazard deliberately.
    """

    @pytest.mark.asyncio
    async def test_a_raising_dependency_cannot_preempt_the_401(self):
        """Re-arms this class now that the route uses the optional provider.

        The two tests below discriminated on wiring only because
        ``get_ban_admin_client`` raised 503. The route now depends on the
        variant that returns None, so on their own they became statements
        about today's providers rather than about the ordering -- they would
        keep passing even if verification moved to a handler parameter.
        Verified by negative control: with the route-level ``dependencies=``
        entry moved into the signature, they still pass and this one fails
        with a 503 naming BS_INTERNAL_BANS_URL.

        The ordering is the property worth pinning, so this injects a
        deliberately-raising dependency and tests it directly.
        """

        def _exploding_dependency():
            raise HTTPException(status_code=503, detail=BAN_ADMIN_UNCONFIGURED_DETAIL)

        payload = _block_actions_payload()
        body = _form_body(payload)
        app = _build_app(slack=_mock_slack_service(), ban_client=_mock_ban_client())
        app.dependency_overrides[get_optional_ban_admin_client] = _exploding_dependency

        resp = await _post(app, body, {"Content-Type": "application/x-www-form-urlencoded"})

        assert resp.status_code == 401
        assert "BS_INTERNAL" not in resp.text

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
    async def test_unauthorized_click_never_opens_the_modal(self):
        """The modal's private_metadata carries the listener's fingerprint, and
        Slack hands the view to the opening client. Authorizing only on submit
        would let any workspace member read a device UUID out of the payload --
        in a repo that truncates fingerprints in logs to avoid exactly that.
        """
        slack = _mock_slack_service()
        payload = _block_actions_payload(user_id="U99UNAUTHORIZED")
        body = _form_body(payload)
        app = _build_app(slack=slack, ban_client=_mock_ban_client())

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.status_code == 200
        slack.open_view.assert_not_awaited()
        slack.post_ephemeral.assert_awaited_once()
        assert "not authorized" in slack.post_ephemeral.call_args.kwargs["text"].lower()

    @pytest.mark.asyncio
    async def test_empty_allowlist_denies_the_click_too(self):
        """Fail-closed applies at click time as well as submit time."""
        slack = _mock_slack_service()
        payload = _block_actions_payload()
        body = _form_body(payload)
        app = _build_app(slack=slack, ban_client=_mock_ban_client(), allowed_users=None)

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.status_code == 200
        slack.open_view.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "payload_json",
        ["[1,2,3]", '"hello"', "null", "42"],
        ids=["list", "string", "null", "number"],
    )
    async def test_valid_json_that_is_not_an_object_is_400_not_500(self, payload_json):
        """Only Slack can produce a valid signature, so this is post-auth --
        but a 500 here is an unhandled exception, and this service's Sentry
        init ships frame locals, so every avoidable 500 on this route is an
        avoidable settings-bearing event.
        """
        slack = _mock_slack_service()
        body = urlencode({"payload": payload_json}).encode()
        app = _build_app(slack=slack, ban_client=_mock_ban_client())

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.status_code == 400
        slack.open_view.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_utf8_body_is_400_not_500(self):
        slack = _mock_slack_service()
        body = b"payload=\xff\xfe"
        app = _build_app(slack=slack, ban_client=_mock_ban_client())

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.status_code == 400

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "user_field",
        [{"id": ["U01ABC"]}, {"id": {"nested": "U01ABC"}}, ["U01ABC"], {"id": 0}, {}],
        ids=["id-list", "id-dict", "user-list", "id-int", "user-empty"],
    )
    async def test_malformed_user_field_is_denied_not_500(self, user_field):
        """`user.id` is json.loads output, so it is Any. An unhashable value
        reaching the allowlist test raises TypeError -- a 500 whose Sentry
        event carries the settings object, which is the failure class the
        authorization check was added to help close, not widen.
        """
        slack = _mock_slack_service()
        payload = _block_actions_payload()
        payload["user"] = user_field
        body = _form_body(payload)
        app = _build_app(slack=slack, ban_client=_mock_ban_client())

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.status_code == 200
        slack.open_view.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_string_fingerprint_in_metadata_skips_cleanly(self):
        """A non-str fingerprint would reach normalize_fingerprint's .strip()
        and raise. Only rom writes private_metadata, so this is unreachable --
        but the skip has to be a refusal, not a crash."""
        slack = _mock_slack_service()
        ban_client = _mock_ban_client()
        payload = _view_submission_payload()
        context = json.loads(payload["view"]["private_metadata"])
        context["fingerprint"] = ["not-a-string"]
        payload["view"]["private_metadata"] = json.dumps(context)
        body = _form_body(payload)
        app = _build_app(slack=slack, ban_client=ban_client)

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.status_code == 200
        ban_client.ban.assert_not_awaited()

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


class TestTheBanAdmin503MovedButDidNotDisappear:
    """``get_optional_ban_admin_client`` never raises, so the refusal for an
    unwired deploy now comes from the handler's ban paths instead.

    The point of the move is blast radius, not leniency: this route is the
    Slack app's single interactivity Request URL, so a raising dependency
    answers *every* callback that ever shares it -- ban-related or not -- with
    a 503 naming two ban-only environment variables. These tests pin that the
    ban flow's own behavior is byte-for-byte what it was: same status, same
    detail, raised at the same two moments.
    """

    @pytest.mark.asyncio
    async def test_button_click_still_503s_with_the_bans_upstream_unset(self):
        """The click, not just the submit.

        Refusing here rather than at submit time is what keeps the move
        behavior-neutral -- and opening a reason modal that cannot possibly
        save would be a worse failure than the 503 it replaced.
        """
        payload = _block_actions_payload()
        body = _form_body(payload)
        app = _build_app(slack=_mock_slack_service(), ban_upstream_configured=False)

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.status_code == 503
        assert "BS_INTERNAL_BANS_URL" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_modal_submission_still_503s_with_the_bans_upstream_unset(self):
        payload = _view_submission_payload()
        body = _form_body(payload)
        app = _build_app(slack=_mock_slack_service(), ban_upstream_configured=False)

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.status_code == 503
        assert "BS_INTERNAL_BANS_URL" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_the_detail_string_is_the_shared_constant(self):
        """One source, so the two providers and the router cannot drift."""
        payload = _view_submission_payload()
        body = _form_body(payload)
        app = _build_app(slack=_mock_slack_service(), ban_upstream_configured=False)

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.json()["detail"] == BAN_ADMIN_UNCONFIGURED_DETAIL

    @pytest.mark.asyncio
    async def test_a_non_ban_click_is_unaffected_by_the_bans_config(self):
        """The half of the blast-radius claim that was previously untested.

        A `block_actions` click for some other button sharing this Request URL
        must not inherit the ban flow's configuration requirements. Verified by
        mutation: hoisting `_require_ban_client` above the ban-button guard --
        which reintroduces exactly the bug this PR fixes -- left the whole
        suite green before this test existed.
        """
        payload = _block_actions_payload(action_id="some_other_button")
        body = _form_body(payload)
        app = _build_app(slack=_mock_slack_service(), ban_upstream_configured=False)

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.status_code == 200
        assert "BS_INTERNAL" not in resp.text

    @pytest.mark.asyncio
    async def test_an_unrelated_modal_submission_is_unaffected_by_the_bans_config(self):
        """The reason the move exists.

        A submission for some other modal sharing this Request URL was a 200
        no-op on a configured deploy and a 503 on an unconfigured one -- purely
        because a dependency resolved before the callback_id guard. It is now a
        200 either way.
        """
        payload = _view_submission_payload()
        payload["view"]["callback_id"] = "some_other_feature_modal"
        body = _form_body(payload)
        app = _build_app(slack=_mock_slack_service(), ban_upstream_configured=False)

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.status_code == 200


class TestMovedDependenciesAreReExportedByIdentity:
    """``verify_slack_request`` and ``get_slack_interactivity_service`` moved to
    core/dependencies.py so a second Slack router can use them without importing
    a sibling router.

    ``dependency_overrides`` is keyed on object identity, so this module must
    re-export *the same object*, not a copy. A copy would leave every existing
    override in this file silently inert -- the tests would pass against a real
    signature check and a real client, and nobody would notice.
    """

    def test_verify_slack_request_is_the_same_object(self):
        from core.dependencies import verify_slack_request as canonical
        from routers.slack_interactivity import verify_slack_request as re_exported

        assert re_exported is canonical

    def test_get_slack_interactivity_service_is_the_same_object(self):
        from core.dependencies import (
            get_slack_interactivity_service as canonical,
        )
        from routers.slack_interactivity import (
            get_slack_interactivity_service as re_exported,
        )

        assert re_exported is canonical


def _mock_moderator_client(current: list[str] | None = None) -> AsyncMock:
    client = AsyncMock()
    client.list_moderators = AsyncMock(return_value=list(current or []))
    return client


class TestBanAuthorizationUsesTheUnion:
    """Who can ban is `SLACK_BAN_AUTHORIZED_USERS` unioned with the BS roster."""

    @pytest.mark.asyncio
    async def test_stored_moderator_can_ban_without_being_in_the_env_allowlist(self):
        stored_only = "U07STORED"
        ban_client = _mock_ban_client()
        payload = _view_submission_payload(user_id=stored_only)
        body = _form_body(payload)
        app = _build_app(
            slack=_mock_slack_service(),
            ban_client=ban_client,
            moderator_client=_mock_moderator_client([stored_only]),
        )

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.status_code == 200
        ban_client.ban.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stored_moderator_can_open_the_ban_modal(self):
        """The click authorizes off the union too, not just the submit."""
        stored_only = "U07STORED"
        slack = _mock_slack_service()
        payload = _block_actions_payload(user_id=stored_only)
        body = _form_body(payload)
        app = _build_app(
            slack=slack,
            ban_client=_mock_ban_client(),
            moderator_client=_mock_moderator_client([stored_only]),
        )

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.status_code == 200
        slack.open_view.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_env_allowlist_still_bans_with_an_empty_roster(self):
        """Day one: the table ships empty and nobody loses access."""
        ban_client = _mock_ban_client()
        payload = _view_submission_payload(user_id=ACTING_USER)
        body = _form_body(payload)
        app = _build_app(
            slack=_mock_slack_service(),
            ban_client=ban_client,
            moderator_client=_mock_moderator_client([]),
        )

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.status_code == 200
        ban_client.ban.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_someone_on_neither_list_still_cannot_ban(self):
        ban_client = _mock_ban_client()
        payload = _view_submission_payload(user_id="U99ZZZ")
        body = _form_body(payload)
        app = _build_app(
            slack=_mock_slack_service(),
            ban_client=ban_client,
            moderator_client=_mock_moderator_client(["U07STORED"]),
        )

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.status_code == 200
        ban_client.ban.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unreachable_roster_shrinks_to_the_env_allowlist(self):
        """Fail closed: a Backend-Service outage must not widen who can ban.

        The direction is the whole point. Reading an upstream error as "allow"
        would turn a BS outage into workspace-wide ban rights, so the roster-only
        user loses access while the break-glass user keeps it.
        """
        stored_only = "U07STORED"
        broken = _mock_moderator_client()
        broken.list_moderators = AsyncMock(
            side_effect=ModeratorClientError(0, {"error": "upstream_unreachable"})
        )

        denied = _mock_ban_client()
        payload = _view_submission_payload(user_id=stored_only)
        body = _form_body(payload)
        app = _build_app(slack=_mock_slack_service(), ban_client=denied, moderator_client=broken)
        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))
        assert resp.status_code == 200
        denied.ban.assert_not_awaited()

        allowlisted = _mock_ban_client()
        payload = _view_submission_payload(user_id=ACTING_USER)
        body = _form_body(payload)
        app = _build_app(
            slack=_mock_slack_service(), ban_client=allowlisted, moderator_client=broken
        )
        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))
        assert resp.status_code == 200
        allowlisted.ban.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_an_unconfigured_roster_upstream_does_not_break_the_ban_button(self):
        """`get_moderator_client` returns None rather than 503ing, precisely so
        that a deploy with no `BS_INTERNAL_MODERATORS_URL` still bans."""
        ban_client = _mock_ban_client()
        payload = _view_submission_payload(user_id=ACTING_USER)
        body = _form_body(payload)
        app = _build_app(
            slack=_mock_slack_service(),
            ban_client=ban_client,
            moderator_client=None,
            moderator_upstream_configured=False,
        )

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.status_code == 200
        ban_client.ban.assert_awaited_once()
