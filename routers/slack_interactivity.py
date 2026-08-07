"""Slack interactivity endpoint for the in-Slack "Ban requester" flow
(request-o-matic#152).

``POST /slack/interactivity`` is the single Request URL Slack calls for every
button click and modal submission across the app (Slack allows exactly one
per app). This router only handles the ban flow; any other interaction type
or callback_id is a silent no-op so a future feature can share the same URL.

Every request is verified with :func:`services.slack_signature.verify_slack_signature`
against the raw body *before* anything else -- an unverified callback is
forgeable, and this one bans people. The flow itself never forks
``services/ban_service.py``: this router is the second (and only other)
caller alongside ``routers/admin.py``, so a Slack click and a curl produce the
same audit trail.

Flow:

1. **block_actions** (button click): read the requester's fingerprint off the
   clicked message's own metadata (never off anything user-supplied), then
   open a reason modal via ``views.open``. The modal's ``private_metadata``
   carries the fingerprint, channel, message ts, and (best-effort, size
   permitting) the message's own blocks minus the ban button -- so the later
   ``chat.update`` can append a footer without losing the original content.
2. **view_submission** (modal submit): re-verify the acting user against the
   allowlist server-side (never trust the client), validate the reason length
   locally (defense in depth -- Slack's own ``plain_text_input`` bounds
   already stop most bad input from reaching here), call
   ``services/ban_service.ban`` with ``actor=None`` (a Slack user ID has no
   better-auth ``user`` row for BS's foreign key -- see
   ``services/ban_service.py``'s module docstring), then post an ephemeral
   ack and edit the original message with a "banned by" footer.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import parse_qs

import httpx
import sentry_sdk
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from config.settings import Settings, get_settings
from core.dependencies import SlackService, get_ban_admin_client, get_http_client
from core.exceptions import SlackPostError
from services import ban_service
from services.ban_admin_client import BanAdminClient, BanAdminClientError
from services.slack import BAN_BUTTON_ACTION_ID, SLACK_METADATA_EVENT_TYPE
from services.slack_authorization import is_authorized_slack_user
from services.slack_signature import verify_slack_signature

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/slack", tags=["slack-interactivity"])

BAN_MODAL_CALLBACK_ID = "ban_reason_modal"
BAN_REASON_BLOCK_ID = "ban_reason_block"
BAN_REASON_ACTION_ID = "ban_reason_input"

REASON_MIN_LENGTH = 1
REASON_MAX_LENGTH = 1000

# Slack caps private_metadata at 3000 characters. When the original message's
# blocks don't fit alongside the fingerprint/channel/ts context, we drop them
# rather than fail the flow -- the ban still succeeds, the edited message just
# degrades to the footer alone instead of footer-plus-original-content.
_MAX_PRIVATE_METADATA_LEN = 3000


async def get_slack_interactivity_service(
    settings: Settings = Depends(get_settings),
    http_client: httpx.AsyncClient = Depends(get_http_client),
) -> SlackService | None:
    """Bot-token-only SlackService for interactivity callbacks.

    Independent of ``SLACK_USE_BOT_TOKEN`` / ``ENABLE_SLACK_INTEGRATION``:
    ``views.open``, ``chat.update``, and ``chat.postEphemeral`` are Web API
    methods with no incoming-webhook equivalent, so this resolves off
    ``SLACK_BOT_TOKEN`` alone rather than reusing ``get_slack_service``'s
    webhook/bot-token transport selection for ``POST /request``.
    """
    if not settings.slack_bot_token:
        return None
    return SlackService(http_client, bot_token=settings.slack_bot_token)


def _redact_fingerprint(fingerprint: str) -> str:
    return f"{fingerprint[:8]}..."


def _is_ban_button_block(block: dict[str, Any]) -> bool:
    if block.get("type") != "actions":
        return False
    return any(el.get("action_id") == BAN_BUTTON_ACTION_ID for el in block.get("elements", []))


def _strip_ban_button(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [b for b in blocks if not _is_ban_button_block(b)]


def _extract_fingerprint(message: dict[str, Any]) -> str | None:
    metadata = message.get("metadata") or {}
    if metadata.get("event_type") != SLACK_METADATA_EVENT_TYPE:
        return None
    fingerprint = metadata.get("event_payload", {}).get("fingerprint")
    return fingerprint if isinstance(fingerprint, str) else None


def _build_ban_reason_modal(private_metadata: str) -> dict[str, Any]:
    return {
        "type": "modal",
        "callback_id": BAN_MODAL_CALLBACK_ID,
        "private_metadata": private_metadata,
        "title": {"type": "plain_text", "text": "Ban requester"},
        "submit": {"type": "plain_text", "text": "Ban"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": BAN_REASON_BLOCK_ID,
                "label": {"type": "plain_text", "text": "Reason"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": BAN_REASON_ACTION_ID,
                    "multiline": True,
                    "min_length": REASON_MIN_LENGTH,
                    "max_length": REASON_MAX_LENGTH,
                },
            }
        ],
    }


def _build_footer_block(user_id: str, reason: str) -> dict[str, Any]:
    return {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": f"\U0001f6ab Banned by <@{user_id}> — {reason}"}],
    }


def _build_updated_blocks(
    original_blocks: list[dict[str, Any]] | None, user_id: str, reason: str
) -> list[dict[str, Any]]:
    footer = _build_footer_block(user_id, reason)
    if original_blocks:
        return [*original_blocks, footer]
    return [footer]


async def _handle_block_actions(payload: dict[str, Any], slack: SlackService | None) -> Response:
    actions = payload.get("actions") or []
    if not any(a.get("action_id") == BAN_BUTTON_ACTION_ID for a in actions):
        return Response(status_code=200)

    message = payload.get("message") or {}
    fingerprint = _extract_fingerprint(message)
    if fingerprint is None:
        logger.warning(
            "slack_interactivity: ban button clicked but message carries no "
            "usable fingerprint metadata; ignoring"
        )
        return Response(status_code=200)

    if slack is None:
        logger.error(
            "slack_interactivity: ban button clicked but SLACK_BOT_TOKEN is "
            "unconfigured; cannot open a modal"
        )
        return Response(status_code=200)

    channel = (payload.get("channel") or {}).get("id")
    message_ts = message.get("ts")
    trigger_id = payload.get("trigger_id")
    if not isinstance(trigger_id, str):
        logger.error("slack_interactivity: block_actions payload missing trigger_id")
        return Response(status_code=200)

    context: dict[str, Any] = {
        "fingerprint": fingerprint,
        "channel": channel,
        "message_ts": message_ts,
        "blocks": _strip_ban_button(message.get("blocks") or []),
    }
    private_metadata = json.dumps(context)
    if len(private_metadata) > _MAX_PRIVATE_METADATA_LEN:
        context["blocks"] = None
        private_metadata = json.dumps(context)

    try:
        await slack.open_view(trigger_id=trigger_id, view=_build_ban_reason_modal(private_metadata))
    except SlackPostError as exc:
        logger.error(
            "slack_interactivity: views.open failed fingerprint=%s error=%s",
            _redact_fingerprint(fingerprint),
            exc,
        )

    return Response(status_code=200)


async def _handle_view_submission(
    payload: dict[str, Any],
    slack: SlackService | None,
    ban_client: BanAdminClient,
    settings: Settings,
) -> Response:
    view = payload.get("view") or {}
    if view.get("callback_id") != BAN_MODAL_CALLBACK_ID:
        return Response(status_code=200)

    user_id = (payload.get("user") or {}).get("id")
    if not isinstance(user_id, str):
        logger.error("slack_interactivity: view_submission payload missing user.id")
        return Response(status_code=200)

    try:
        context = json.loads(view.get("private_metadata") or "{}")
    except json.JSONDecodeError:
        logger.error("slack_interactivity: malformed private_metadata on view_submission")
        return Response(status_code=200)

    fingerprint = context.get("fingerprint")
    channel = context.get("channel")
    message_ts = context.get("message_ts")
    original_blocks = context.get("blocks")

    if not is_authorized_slack_user(user_id, settings.slack_ban_authorized_users):
        logger.warning("slack_interactivity: unauthorized ban attempt by user=%s", user_id)
        sentry_sdk.add_breadcrumb(
            category="slack_ban",
            level="warning",
            message="Unauthorized ban attempt via Slack interactivity",
            data={"user_id": user_id},
        )
        if slack is not None and channel:
            await slack.post_ephemeral(
                channel=channel,
                user=user_id,
                text="You are not authorized to ban requesters.",
            )
        return Response(status_code=200)

    if not fingerprint or not channel or not message_ts:
        logger.error(
            "slack_interactivity: view_submission missing fingerprint/channel/message_ts "
            "in private_metadata; cannot proceed"
        )
        return Response(status_code=200)

    reason = (
        view.get("state", {})
        .get("values", {})
        .get(BAN_REASON_BLOCK_ID, {})
        .get(BAN_REASON_ACTION_ID, {})
        .get("value")
    )
    if reason is None or not (REASON_MIN_LENGTH <= len(reason) <= REASON_MAX_LENGTH):
        return JSONResponse(
            content={
                "response_action": "errors",
                "errors": {
                    BAN_REASON_BLOCK_ID: (
                        f"Reason must be between {REASON_MIN_LENGTH} and "
                        f"{REASON_MAX_LENGTH} characters."
                    )
                },
            }
        )

    # actor=None, not the Slack user ID: BS's banned_by_user_id references
    # better-auth's user.id, which a Slack user has no row in -- passing the
    # raw Slack ID here would 400 on BS's FK check on every single ban (see
    # services/ban_service.py's module docstring). The acting user is still
    # recorded, just in Slack itself via the ephemeral ack and edited footer
    # below, so log it here too for rom's own observability.
    logger.info(
        "slack_interactivity: user=%s banning fingerprint=%s reason_len=%d",
        user_id,
        _redact_fingerprint(fingerprint),
        len(reason),
    )
    try:
        await ban_service.ban(ban_client, fingerprint=fingerprint, reason=reason, actor=None)
    except BanAdminClientError as exc:
        logger.warning(
            "slack_interactivity: ban failed fingerprint=%s status=%d body=%r",
            _redact_fingerprint(fingerprint),
            exc.status_code,
            exc.body,
        )
        if slack is not None:
            await slack.post_ephemeral(
                channel=channel,
                user=user_id,
                text="Ban failed -- Backend-Service rejected the request. Check the logs.",
            )
        return Response(status_code=200)

    if slack is not None:
        await slack.post_ephemeral(
            channel=channel,
            user=user_id,
            text=f"Banned. Reason: {reason}",
        )
        try:
            await slack.update_message(
                channel=channel,
                ts=message_ts,
                blocks=_build_updated_blocks(original_blocks, user_id, reason),
            )
        except SlackPostError as exc:
            logger.error(
                "slack_interactivity: chat.update failed fingerprint=%s error=%s",
                _redact_fingerprint(fingerprint),
                exc,
            )

    return Response(status_code=200)


@router.post(
    "/interactivity",
    summary="Slack interactivity callback (button clicks + modal submissions)",
    description=(
        "The single Request URL for this Slack app's interactive components. "
        "Handles the 'Ban requester' button (#152): a block_actions click opens "
        "a reason modal, and the resulting view_submission bans the fingerprint "
        "carried in the original message's metadata."
    ),
    responses={
        200: {"description": "Acknowledged (always, regardless of internal handling)"},
        401: {"description": "Invalid or missing Slack request signature"},
    },
)
async def slack_interactivity(
    request: Request,
    settings: Settings = Depends(get_settings),
    slack: SlackService | None = Depends(get_slack_interactivity_service),
    ban_client: BanAdminClient = Depends(get_ban_admin_client),
    x_slack_request_timestamp: str | None = Header(default=None),
    x_slack_signature: str | None = Header(default=None),
) -> Response:
    """POST /slack/interactivity -- verify, then dispatch by interaction type."""
    raw_body = await request.body()

    if not verify_slack_signature(
        settings.slack_signing_secret,
        timestamp=x_slack_request_timestamp,
        body=raw_body,
        signature=x_slack_signature,
    ):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")

    form = parse_qs(raw_body.decode("utf-8"))
    payload_values = form.get("payload")
    if not payload_values:
        raise HTTPException(status_code=400, detail="Missing payload")

    try:
        payload = json.loads(payload_values[0])
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Malformed payload") from exc

    interaction_type = payload.get("type")
    if interaction_type == "block_actions":
        return await _handle_block_actions(payload, slack)
    if interaction_type == "view_submission":
        return await _handle_view_submission(payload, slack, ban_client, settings)

    logger.info("slack_interactivity: ignoring unhandled interaction type=%s", interaction_type)
    return Response(status_code=200)
