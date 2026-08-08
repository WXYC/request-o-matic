"""Unit tests for routers/slack_commands.py -- POST /slack/commands (#240).

Donor: tests/unit/test_slack_interactivity_router.py. Slash commands arrive as
``application/x-www-form-urlencoded`` exactly like interactivity payloads, and
the signature is HMAC'd over the same raw body -- the only real difference is
that the fields are top-level rather than a single JSON ``payload`` value.

Three of the classes below exist for traps rather than features: the
dependency-ordering trap (config state leaking to unauthenticated callers), the
silent-refusal trap (refusals that need a bot token to be seen), and the retry
trap (a retry that fires on the wrong exceptions makes a bad situation worse).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from config.settings import Settings, get_settings
from core.dependencies import get_moderator_client, get_slack_interactivity_service
from core.exceptions import SlackPostError
from routers.slack_commands import MODS_COMMAND, router
from routers.slack_interactivity import (
    MODERATOR_ACTION_ID,
    MODERATOR_BLOCK_ID,
    MODERATOR_MODAL_CALLBACK_ID,
)
from services.moderator_client import ModeratorClientError

SIGNING_SECRET = "test-signing-secret"
TRIGGER_ID = "trigger-abc123"
ACTING_USER = "U01ABC"
MOD_ONE = "U01ABCDEF"
MOD_TWO = "U02GHIJKL"


def _sign(secret: str, timestamp: str, body: bytes) -> str:
    basestring = f"v0:{timestamp}:".encode() + body
    digest = hmac.new(secret.encode("utf-8"), basestring, hashlib.sha256).hexdigest()
    return f"v0={digest}"


def _command_body(
    *, command: str = MODS_COMMAND, user_id: str = ACTING_USER, trigger_id: str = TRIGGER_ID
) -> bytes:
    return urlencode(
        {
            "command": command,
            "text": "",
            "trigger_id": trigger_id,
            "user_id": user_id,
            "team_id": "T0123456",
            "channel_id": "C0123456",
        }
    ).encode()


def _signed_headers(secret: str, body: bytes, *, timestamp: str | None = None) -> dict[str, str]:
    ts = timestamp or str(int(time.time()))
    return {
        "X-Slack-Request-Timestamp": ts,
        "X-Slack-Signature": _sign(secret, ts, body),
        "Content-Type": "application/x-www-form-urlencoded",
    }


def _mock_slack_service() -> AsyncMock:
    service = AsyncMock()
    service.open_view = AsyncMock(return_value=None)
    return service


def _mock_moderator_client(current: list[str] | None = None) -> AsyncMock:
    client = AsyncMock()
    client.list_moderators = AsyncMock(return_value=list(current or []))
    return client


def _build_app(
    *,
    signing_secret: str | None = SIGNING_SECRET,
    allowed_users: str | None = ACTING_USER,
    slack: AsyncMock | None = None,
    moderator_client: AsyncMock | None = None,
    moderator_upstream_configured: bool = True,
    slack_bot_token: str | None = "xoxb-test",
) -> FastAPI:
    app = FastAPI()
    app.include_router(router)

    settings = Settings(
        groq_api_key="test_groq_key",
        slack_signing_secret=signing_secret,
        slack_ban_authorized_users=allowed_users,
        slack_bot_token=slack_bot_token,
        bs_internal_moderators_url=(
            "https://bs.example.com/internal/slack-ban-moderators"
            if moderator_upstream_configured
            else None
        ),
        bs_internal_key="test-internal-key" if moderator_upstream_configured else None,
    )
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_slack_interactivity_service] = lambda: slack
    app.dependency_overrides[get_moderator_client] = lambda: moderator_client
    return app


async def _post(app: FastAPI, body: bytes, headers: dict[str, str]):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.post("/slack/commands", content=body, headers=headers)


def _opened_view(slack: AsyncMock) -> Any:
    """The view payload passed to the most recent views.open call."""
    return slack.open_view.await_args[1]["view"]


def _picker_element(view: Any) -> Any:
    """The multi_users_select element out of the roster block."""
    for block in view["blocks"]:
        if block.get("block_id") == MODERATOR_BLOCK_ID:
            return block["element"]
    raise AssertionError(f"no {MODERATOR_BLOCK_ID} block in view")


class TestSignatureVerification:
    @pytest.mark.asyncio
    async def test_valid_signature_is_accepted(self):
        body = _command_body()
        app = _build_app(slack=_mock_slack_service(), moderator_client=_mock_moderator_client())

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_wrong_signature_rejected_401(self):
        body = _command_body()
        app = _build_app(slack=_mock_slack_service(), moderator_client=_mock_moderator_client())
        headers = _signed_headers(SIGNING_SECRET, body)
        headers["X-Slack-Signature"] = "v0=" + "0" * 64

        assert (await _post(app, body, headers)).status_code == 401

    @pytest.mark.asyncio
    async def test_missing_signature_headers_rejected_401(self):
        body = _command_body()
        app = _build_app(slack=_mock_slack_service(), moderator_client=_mock_moderator_client())

        resp = await _post(app, body, {"Content-Type": "application/x-www-form-urlencoded"})

        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_stale_timestamp_rejected_401(self):
        body = _command_body()
        app = _build_app(slack=_mock_slack_service(), moderator_client=_mock_moderator_client())
        stale = str(int(time.time()) - 3600)

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body, timestamp=stale))

        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_unauthenticated_request_never_opens_a_modal(self):
        slack = _mock_slack_service()
        body = _command_body()
        app = _build_app(slack=slack, moderator_client=_mock_moderator_client())

        await _post(app, body, {"Content-Type": "application/x-www-form-urlencoded"})

        slack.open_view.assert_not_awaited()


class TestSignatureIsCheckedBeforeAnythingElse:
    """The dependency-ordering trap this route inherits from its sibling.

    ``verify_slack_request`` must be wired as a route-level
    ``dependencies=[...]`` entry, never as a handler parameter. FastAPI resolves
    route-level dependencies first; as a parameter this would resolve *after*
    ``get_moderator_client``, and an unsigned POST to a deployment missing the
    roster configuration would come back carrying that state.
    """

    @pytest.mark.asyncio
    async def test_unsigned_request_to_unconfigured_deployment_is_401(self):
        body = _command_body()
        app = _build_app(
            slack=_mock_slack_service(),
            moderator_client=None,
            moderator_upstream_configured=False,
        )

        resp = await _post(app, body, {"Content-Type": "application/x-www-form-urlencoded"})

        assert resp.status_code == 401
        assert "BS_INTERNAL" not in resp.text
        assert "MODERATORS" not in resp.text

    @pytest.mark.asyncio
    async def test_a_raising_dependency_cannot_preempt_the_401(self):
        """The test that actually discriminates on wiring.

        The case above passes either way, because none of this route's real
        dependencies raise -- ``get_moderator_client`` returns None rather than
        503ing, deliberately. That makes it a statement about today's providers,
        not about the ordering, and the ordering is the property worth pinning:
        the sibling route's 503 leak was possible precisely because a *future*
        dependency did raise.

        So this injects one. With ``verify_slack_request`` as a route-level
        entry the 401 always wins; as a handler parameter it wins only if it
        happens to be declared first, which is not a property to rely on.
        """
        from fastapi import HTTPException

        def _exploding_dependency():
            raise HTTPException(
                status_code=503,
                detail="Moderator upstream not configured: set BS_INTERNAL_MODERATORS_URL",
            )

        body = _command_body()
        app = _build_app(slack=_mock_slack_service(), moderator_client=_mock_moderator_client())
        app.dependency_overrides[get_moderator_client] = _exploding_dependency

        resp = await _post(app, body, {"Content-Type": "application/x-www-form-urlencoded"})

        assert resp.status_code == 401
        assert "BS_INTERNAL_MODERATORS_URL" not in resp.text

    @pytest.mark.asyncio
    async def test_unsigned_request_looks_identical_configured_or_not(self):
        body = _command_body()
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        configured = await _post(
            _build_app(slack=_mock_slack_service(), moderator_client=_mock_moderator_client()),
            body,
            headers,
        )
        unconfigured = await _post(
            _build_app(
                slack=_mock_slack_service(),
                moderator_client=None,
                moderator_upstream_configured=False,
            ),
            body,
            headers,
        )

        assert configured.status_code == unconfigured.status_code == 401
        assert configured.text == unconfigured.text


class TestOpensThePicker:
    @pytest.mark.asyncio
    async def test_initial_users_matches_the_stored_roster(self):
        slack = _mock_slack_service()
        body = _command_body()
        app = _build_app(slack=slack, moderator_client=_mock_moderator_client([MOD_ONE, MOD_TWO]))

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.status_code == 200
        slack.open_view.assert_awaited_once()
        assert slack.open_view.await_args[1]["trigger_id"] == TRIGGER_ID
        assert _picker_element(_opened_view(slack))["initial_users"] == [MOD_ONE, MOD_TWO]

    @pytest.mark.asyncio
    async def test_view_carries_the_moderator_callback_id(self):
        """The submission is dispatched on this id by the interactivity router."""
        slack = _mock_slack_service()
        body = _command_body()
        app = _build_app(slack=slack, moderator_client=_mock_moderator_client([MOD_ONE]))

        await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert _opened_view(slack)["callback_id"] == MODERATOR_MODAL_CALLBACK_ID

    @pytest.mark.asyncio
    async def test_private_metadata_carries_the_roster_that_was_read(self):
        """This is the expectedCurrent the save round-trips back to BS."""
        slack = _mock_slack_service()
        body = _command_body()
        app = _build_app(slack=slack, moderator_client=_mock_moderator_client([MOD_TWO, MOD_ONE]))

        await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        metadata = json.loads(_opened_view(slack)["private_metadata"])
        assert metadata["moderators"] == [MOD_TWO, MOD_ONE]

    @pytest.mark.asyncio
    async def test_picker_is_optional_so_removing_everyone_is_submittable(self):
        """Without optional:true Slack blocks submitting an empty selection,
        making "remove the last moderator" impossible from the only UI that
        can remove moderators."""
        slack = _mock_slack_service()
        body = _command_body()
        app = _build_app(slack=slack, moderator_client=_mock_moderator_client([MOD_ONE]))

        await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        block = next(
            b for b in _opened_view(slack)["blocks"] if b.get("block_id") == MODERATOR_BLOCK_ID
        )
        assert block["optional"] is True
        assert _picker_element(_opened_view(slack))["action_id"] == MODERATOR_ACTION_ID

    @pytest.mark.asyncio
    async def test_empty_roster_omits_initial_users_entirely(self):
        """Slack rejects an empty initial_users array, and day one is empty."""
        slack = _mock_slack_service()
        body = _command_body()
        app = _build_app(slack=slack, moderator_client=_mock_moderator_client([]))

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.status_code == 200
        assert "initial_users" not in _picker_element(_opened_view(slack))

    @pytest.mark.asyncio
    async def test_reads_the_roster_exactly_once(self):
        """Authorization and initial_users share one read.

        trigger_id dies at ~3 seconds with a views.open still to follow, so the
        budget is one upstream round-trip and no more.
        """
        moderators = _mock_moderator_client([MOD_ONE])
        body = _command_body()
        app = _build_app(slack=_mock_slack_service(), moderator_client=moderators)

        await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert moderators.list_moderators.await_count == 1


class TestBreakGlassVisibility:
    """The modal must never understate who can ban.

    Authorization is the union, so initial_users alone understates it -- most
    starkly on day one, when the table is empty and the environment allowlist
    is doing all the work.
    """

    @pytest.mark.asyncio
    async def test_context_block_names_environment_allowlist_members(self):
        slack = _mock_slack_service()
        body = _command_body()
        app = _build_app(
            slack=slack,
            allowed_users=f"{ACTING_USER},U05BREAK",
            moderator_client=_mock_moderator_client([MOD_ONE]),
        )

        await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        rendered = json.dumps(_opened_view(slack))
        assert "U05BREAK" in rendered
        assert "SLACK_BAN_AUTHORIZED_USERS" in rendered

    @pytest.mark.asyncio
    async def test_empty_table_still_shows_who_can_ban(self):
        """Day one is the starkest case: nobody in the picker, five people
        able to ban."""
        slack = _mock_slack_service()
        body = _command_body()
        app = _build_app(
            slack=slack,
            allowed_users=f"{ACTING_USER},U05BREAK",
            moderator_client=_mock_moderator_client([]),
        )

        await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        rendered = json.dumps(_opened_view(slack))
        assert "U05BREAK" in rendered
        assert "initial_users" not in rendered

    @pytest.mark.asyncio
    async def test_no_break_glass_block_when_the_env_allowlist_is_empty(self):
        """After the break-glass trim reaches zero there is nothing to disclose.

        The invoker here is authorized by the table alone, which is the whole
        point of the trim.
        """
        slack = _mock_slack_service()
        body = _command_body(user_id=MOD_ONE)
        app = _build_app(
            slack=slack, allowed_users=None, moderator_client=_mock_moderator_client([MOD_ONE])
        )

        await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert "SLACK_BAN_AUTHORIZED_USERS" not in json.dumps(_opened_view(slack))


class TestAuthorization:
    @pytest.mark.asyncio
    async def test_unauthorized_invoker_gets_an_ephemeral_and_no_modal(self):
        slack = _mock_slack_service()
        body = _command_body(user_id="U99ZZZ")
        app = _build_app(slack=slack, moderator_client=_mock_moderator_client([MOD_ONE]))

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.status_code == 200
        assert resp.json()["response_type"] == "ephemeral"
        slack.open_view.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_refusal_does_not_enumerate_the_roster(self):
        """The roster isn't secret, but it shouldn't be casually enumerable by
        the whole workspace -- same posture as the ban button's pre-modal check.
        """
        body = _command_body(user_id="U99ZZZ")
        app = _build_app(
            slack=_mock_slack_service(), moderator_client=_mock_moderator_client([MOD_ONE, MOD_TWO])
        )

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert MOD_ONE not in resp.text
        assert MOD_TWO not in resp.text

    @pytest.mark.asyncio
    async def test_stored_moderator_may_open_without_being_in_the_env_allowlist(self):
        slack = _mock_slack_service()
        stored_only = "U07STORED"
        body = _command_body(user_id=stored_only)
        app = _build_app(slack=slack, moderator_client=_mock_moderator_client([stored_only]))

        await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        slack.open_view.assert_awaited_once()


class TestRefusalsAreAlwaysVisible:
    """Every refusal is a 200 with an ephemeral JSON body, NOT chat.postEphemeral.

    The interactivity router's refusals go through the Web API and are guarded
    by ``if slack is not None``, because that dependency returns None without
    SLACK_BOT_TOKEN. Reusing that shape here would make a bot-token-less deploy
    refuse *silently*. A slash command's response body reaches the invoker
    unconditionally, so it never needs the Web API to say no.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("user_id", "moderator_client", "configured"),
        [
            ("U99ZZZ", _mock_moderator_client([MOD_ONE]), True),
            (ACTING_USER, None, False),
        ],
        ids=["unauthorized", "upstream-unconfigured"],
    )
    async def test_refusal_is_visible_without_a_bot_token(
        self, user_id, moderator_client, configured
    ):
        body = _command_body(user_id=user_id)
        app = _build_app(
            slack=None,
            slack_bot_token=None,
            moderator_client=moderator_client,
            moderator_upstream_configured=configured,
        )

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.status_code == 200
        assert resp.json()["response_type"] == "ephemeral"
        assert resp.json()["text"]

    @pytest.mark.asyncio
    async def test_unconfigured_upstream_refuses_rather_than_opening_an_unsavable_modal(self):
        slack = _mock_slack_service()
        body = _command_body()
        app = _build_app(slack=slack, moderator_client=None, moderator_upstream_configured=False)

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.status_code == 200
        assert resp.json()["response_type"] == "ephemeral"
        slack.open_view.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unreachable_upstream_refuses_rather_than_opening_an_empty_picker(self):
        """An error must not render as "the roster is empty" -- saving that
        would wipe it."""
        slack = _mock_slack_service()
        moderators = _mock_moderator_client()
        moderators.list_moderators = AsyncMock(
            side_effect=ModeratorClientError(0, {"error": "upstream_unreachable"})
        )
        body = _command_body()
        app = _build_app(slack=slack, moderator_client=moderators)

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.status_code == 200
        assert resp.json()["response_type"] == "ephemeral"
        slack.open_view.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_slack_service_refuses_visibly(self):
        body = _command_body()
        app = _build_app(
            slack=None, slack_bot_token=None, moderator_client=_mock_moderator_client([MOD_ONE])
        )

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.status_code == 200
        assert resp.json()["response_type"] == "ephemeral"


class TestStaleInitialUsersRetry:
    """Insurance against a deactivated account making the roster uneditable.

    The premise is semester turnover, so deactivated Slack IDs will accumulate,
    and Slack validates initial_users when it opens the view. If a stale ID can
    fail views.open, the roster becomes uneditable by the only tool that could
    remove it.
    """

    @pytest.mark.asyncio
    async def test_payload_rejection_retries_once_without_initial_users(self):
        slack = _mock_slack_service()
        slack.open_view = AsyncMock(
            side_effect=[SlackPostError("Slack views.open failed: invalid_arguments"), None]
        )
        body = _command_body()
        app = _build_app(slack=slack, moderator_client=_mock_moderator_client([MOD_ONE, MOD_TWO]))

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.status_code == 200
        assert slack.open_view.await_count == 2
        retried_view = slack.open_view.await_args_list[1][1]["view"]
        assert "initial_users" not in _picker_element(retried_view)

    @pytest.mark.asyncio
    async def test_retry_names_the_dropped_ids(self):
        slack = _mock_slack_service()
        slack.open_view = AsyncMock(
            side_effect=[SlackPostError("Slack views.open failed: invalid_arguments"), None]
        )
        body = _command_body()
        app = _build_app(slack=slack, moderator_client=_mock_moderator_client([MOD_ONE]))

        await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert MOD_ONE in json.dumps(slack.open_view.await_args_list[1][1]["view"])

    @pytest.mark.asyncio
    async def test_retry_preserves_private_metadata(self):
        """The retry still has to carry expectedCurrent, or the save can't
        detect a concurrent edit."""
        slack = _mock_slack_service()
        slack.open_view = AsyncMock(
            side_effect=[SlackPostError("Slack views.open failed: invalid_arguments"), None]
        )
        body = _command_body()
        app = _build_app(slack=slack, moderator_client=_mock_moderator_client([MOD_ONE]))

        await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        retried = slack.open_view.await_args_list[1][1]["view"]
        assert json.loads(retried["private_metadata"])["moderators"] == [MOD_ONE]

    @pytest.mark.asyncio
    async def test_expired_trigger_id_is_not_retried(self):
        """Retrying a dead trigger_id is guaranteed to fail again."""
        slack = _mock_slack_service()
        slack.open_view = AsyncMock(
            side_effect=SlackPostError("Slack views.open failed: expired_trigger_id")
        )
        body = _command_body()
        app = _build_app(slack=slack, moderator_client=_mock_moderator_client([MOD_ONE]))

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert slack.open_view.await_count == 1
        assert resp.json()["response_type"] == "ephemeral"

    @pytest.mark.asyncio
    async def test_timeout_surfaces_as_a_refusal_and_does_not_consume_the_retry(self):
        """A timeout spends a full budget on a window that has already closed.

        Conditioning the retry on SlackPostError rather than on any exception
        is what keeps this from costing a second one.
        """
        slack = _mock_slack_service()
        slack.open_view = AsyncMock(side_effect=httpx.TimeoutException("views.open timed out"))
        body = _command_body()
        app = _build_app(slack=slack, moderator_client=_mock_moderator_client([MOD_ONE]))

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.status_code == 200
        assert slack.open_view.await_count == 1
        assert resp.json()["response_type"] == "ephemeral"

    @pytest.mark.asyncio
    async def test_retry_timing_out_refuses_rather_than_500ing(self):
        """A timeout on the *retry* must not escape as a 500.

        The retry only happens when Slack is already misbehaving, so a timeout
        on the second attempt is one of the likelier outcomes -- not an exotic
        one. It is also the case an `except SlackPostError` handler cannot
        delegate to a sibling `except Exception` on the same try: a handler
        never catches what another handler raises, so the inner one has to
        cover it itself. Otherwise this route returns a 500, and an unhandled
        500 here ships the frame's locals (settings included) to Sentry.
        """
        slack = _mock_slack_service()
        slack.open_view = AsyncMock(
            side_effect=[
                SlackPostError("Slack views.open failed: invalid_arguments"),
                httpx.TimeoutException("views.open timed out"),
            ]
        )
        body = _command_body()
        app = _build_app(slack=slack, moderator_client=_mock_moderator_client([MOD_ONE]))

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.status_code == 200
        assert slack.open_view.await_count == 2
        assert resp.json()["response_type"] == "ephemeral"

    @pytest.mark.asyncio
    async def test_second_failure_refuses_rather_than_500ing(self):
        slack = _mock_slack_service()
        slack.open_view = AsyncMock(
            side_effect=[
                SlackPostError("Slack views.open failed: invalid_arguments"),
                SlackPostError("Slack views.open failed: invalid_arguments"),
            ]
        )
        body = _command_body()
        app = _build_app(slack=slack, moderator_client=_mock_moderator_client([MOD_ONE]))

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.status_code == 200
        assert slack.open_view.await_count == 2
        assert resp.json()["response_type"] == "ephemeral"


class TestUnknownCommand:
    @pytest.mark.asyncio
    async def test_unknown_command_opens_nothing(self):
        slack = _mock_slack_service()
        body = _command_body(command="/some-other-command")
        app = _build_app(slack=slack, moderator_client=_mock_moderator_client([MOD_ONE]))

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.status_code == 200
        slack.open_view.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_trigger_id_refuses_without_calling_slack(self):
        slack = _mock_slack_service()
        body = _command_body(trigger_id="")
        app = _build_app(slack=slack, moderator_client=_mock_moderator_client([MOD_ONE]))

        resp = await _post(app, body, _signed_headers(SIGNING_SECRET, body))

        assert resp.status_code == 200
        slack.open_view.assert_not_awaited()
