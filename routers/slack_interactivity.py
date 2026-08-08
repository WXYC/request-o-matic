"""Slack interactivity endpoint for the in-Slack "Ban requester" flow
(request-o-matic#152).

``POST /slack/interactivity`` is the single Request URL Slack calls for every
button click and modal submission across the app (Slack allows exactly one
per app). It handles two flows -- the ban modal (#152) and the moderator
roster save (#240) -- and any other interaction type or callback_id is a
silent 200 no-op so a future feature can share the same URL.

The roster modal is *opened* by ``routers/slack_commands.py`` but *saved*
here, because its ``view_submission`` has nowhere else to arrive.

Every request is verified with :func:`services.slack_signature.verify_slack_signature`
against the raw body *before* anything else -- an unverified callback is
forgeable, and this one bans people. "Before anything else" is enforced by
running that check as a route-level dependency (:func:`verify_slack_request`),
which FastAPI resolves ahead of the handler's own dependencies; an unsigned
request therefore cannot reach -- or learn anything from -- the ban-admin
client's own configuration check, which lives in the handler's ban paths
rather than on its dependencies (see :func:`_require_ban_client`). The flow
itself never forks ``services/ban_service.py``: this router is the second (and
only other) caller alongside ``routers/admin.py``, so a Slack click and a curl
produce the same audit trail.

Flow:

1. **block_actions** (button click): read the requester's fingerprint off the
   clicked message's own metadata (never off anything user-supplied), then
   open a reason modal via ``views.open``. The modal's ``private_metadata``
   carries the fingerprint, channel, message ts, and (size permitting) the
   message's own blocks minus the ban button -- so the later ``chat.update``
   can append a footer without losing the original content. When they don't
   fit, the footer is skipped rather than sent alone; see
   ``_MAX_PRIVATE_METADATA_LEN``.
2. **view_submission** (modal submit): re-verify the acting user against the
   authorized set server-side (never trust the client), validate the reason
   length locally (defense in depth -- Slack's own ``plain_text_input`` bounds
   already stop most bad input from reaching here), call
   ``services/ban_service.ban`` with ``actor=None`` (a Slack user ID has no
   better-auth ``user`` row for BS's foreign key -- see
   ``services/ban_service.py``'s module docstring), then post an ephemeral
   ack and edit the original message with a "banned by" footer.

"Against the allowlist" became "against the union" in #240: the authorized set
is now ``SLACK_BAN_AUTHORIZED_USERS`` (break-glass) unioned with the roster
stored in Backend-Service. Both authorization call sites are ``await``ed for
that reason; the call-site *contract* is otherwise unchanged, which is what
``services/slack_authorization.py`` predicted when this was a static list. An
unreachable roster shrinks the authorized set to the break-glass list, never
widens it.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import parse_qs

import httpx
import sentry_sdk
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from config.settings import Settings, get_settings
from core.dependencies import (
    BAN_ADMIN_UNCONFIGURED_DETAIL,
    SlackService,
    get_moderator_client,
    get_optional_ban_admin_client,
    get_slack_interactivity_service,
    verify_slack_request,
)
from core.exceptions import SlackPostError
from services import ban_service
from services.ban_admin_client import BanAdminClient, BanAdminClientError
from services.fingerprint import normalize_fingerprint
from services.moderator_client import ModeratorClient, ModeratorClientError
from services.slack import (
    BAN_BUTTON_ACTION_ID,
    MAX_PRIVATE_METADATA_LEN,
    SLACK_METADATA_EVENT_TYPE,
)
from services.slack_authorization import is_authorized_slack_user_in, resolve_authorized_users

logger = logging.getLogger(__name__)

# Re-exported, NOT redefined. ``verify_slack_request`` and
# ``get_slack_interactivity_service`` moved to core/dependencies.py so a second
# Slack router (#240) can use them without importing a sibling router.
#
# The re-export must stay the same object rather than a copy, because
# ``dependency_overrides`` is keyed on identity. To be precise about when that
# bites: a copy would NOT break this module's own tests, which import the name
# from here and thus override whatever this module holds. It bites once a
# second router overrides the ``core.dependencies`` name -- then one override
# would silently miss this route. That is the near-miss the optional/raising
# ban-client pair actually hit during this work, and the reason it is pinned
# here before the second router exists rather than after.
__all__ = [
    "BAN_MODAL_CALLBACK_ID",
    "BAN_REASON_ACTION_ID",
    "BAN_REASON_BLOCK_ID",
    "get_slack_interactivity_service",
    "router",
    "verify_slack_request",
]

router = APIRouter(prefix="/slack", tags=["slack-interactivity"])

BAN_MODAL_CALLBACK_ID = "ban_reason_modal"
BAN_REASON_BLOCK_ID = "ban_reason_block"
BAN_REASON_ACTION_ID = "ban_reason_input"

# The /request-mods roster modal (#240). Defined here rather than in the module
# that opens it, because this module is where the submission is dispatched --
# and a callback_id whose producer and consumer sit in different modules is
# exactly the pair that drifts.
MODERATOR_MODAL_CALLBACK_ID = "moderator_roster_modal"
MODERATOR_BLOCK_ID = "moderator_roster_block"
MODERATOR_ACTION_ID = "moderator_roster_select"

REASON_MIN_LENGTH = 1
REASON_MAX_LENGTH = 1000

# Alias for the ceiling that moved to services/slack.py so a second modal could
# share it without importing this router's underscored name. When the original
# message's blocks don't fit alongside the fingerprint/channel/ts context we
# stash None, and the ban branch skips the chat.update entirely rather than
# sending the footer alone: chat.update replaces a message's blocks wholesale,
# so a footer-only edit would delete the request post the channel is looking at.
# Re-fetching the blocks at submit time instead would need conversations.history
# (channels:history), a scope this app doesn't have -- so the long-post case
# gives up the footer to keep the post, never the other way round.
_MAX_PRIVATE_METADATA_LEN = MAX_PRIVATE_METADATA_LEN


def _redact_fingerprint(fingerprint: str) -> str:
    return f"{fingerprint[:8]}..."


def _is_ban_button_block(block: dict[str, Any]) -> bool:
    if block.get("type") != "actions":
        return False
    return any(el.get("action_id") == BAN_BUTTON_ACTION_ID for el in block.get("elements", []))


def _strip_ban_button(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [b for b in blocks if not _is_ban_button_block(b)]


def _field(payload: dict[str, Any], container: str, key: str) -> str | None:
    """Read ``payload[container][key]`` as a string, or None for any other shape.

    Interaction payloads are ``json.loads`` output, so every nested value is
    ``Any``. Indexing a value that turned out to be a list or a scalar raises,
    and on this route an uncaught exception is a 500 whose Sentry event carries
    the settings object. Only Slack can produce a payload that gets this far, so
    a malformed shape is not attacker-reachable -- but the cost of tolerating it
    is one isinstance check.
    """
    nested = payload.get(container)
    if not isinstance(nested, dict):
        return None
    value = nested.get(key)
    return value if isinstance(value, str) else None


def _nested_dict(container: Any, *keys: str) -> dict[str, Any]:
    """Walk ``container`` through ``keys``, returning {} at the first non-dict.

    The chained ``.get(k, {})`` idiom this replaces only guards *absent* keys:
    a key present with a non-dict value returns that value, and the next
    ``.get`` on it raises ``AttributeError``. Interaction payloads are
    ``json.loads`` output, so every level is ``Any`` -- and on this route an
    uncaught exception is a 500 whose Sentry event carries the settings object,
    which holds every secret the process has. Only Slack can produce a
    signature-verified payload that gets this far, so none of these shapes is
    attacker-reachable; the cost of tolerating them is one isinstance check per
    level, which is the same trade ``_field`` already makes.
    """
    current = container
    for key in keys:
        if not isinstance(current, dict):
            return {}
        current = current.get(key)
    return current if isinstance(current, dict) else {}


def _extract_fingerprint(message: dict[str, Any]) -> str | None:
    metadata = message.get("metadata") or {}
    if metadata.get("event_type") != SLACK_METADATA_EVENT_TYPE:
        return None
    event_payload = metadata.get("event_payload") or {}
    fingerprint = event_payload.get("fingerprint")
    return fingerprint if isinstance(fingerprint, str) else None


async def _notify_slack(coro: Any, *, description: str) -> None:
    """Await a best-effort Slack notification, swallowing any failure.

    Every call site using this helper runs *after* the authoritative action
    for the request has already been decided (a ban that already landed in
    BS, or an authorization refusal that's already final) -- letting a
    notification failure (``SlackPostError`` on ``{"ok": false}``, or a raw
    ``httpx.HTTPError`` from ``raise_for_status``) propagate to a 500 here
    would tell Slack the whole interaction failed and risk a retried
    ``view_submission`` for an action that already succeeded.
    """
    try:
        await coro
    except (SlackPostError, httpx.HTTPError) as exc:
        logger.error("slack_interactivity: %s failed: %s", description, exc)


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
    original_blocks: list[dict[str, Any]], user_id: str, reason: str
) -> list[dict[str, Any]]:
    """Append the ban footer to the message's own blocks.

    ``original_blocks`` is deliberately non-optional: chat.update replaces
    blocks wholesale, so a footer-only result would erase the request post.
    Callers with nothing to append to must skip the edit instead (see
    ``_MAX_PRIVATE_METADATA_LEN``).
    """
    return [*original_blocks, _build_footer_block(user_id, reason)]


def _require_ban_client(ban_client: BanAdminClient | None) -> BanAdminClient:
    """Raise the ban-admin 503 that used to live on the dependency.

    ``get_optional_ban_admin_client`` returns None rather than raising so an
    unsigned request -- or a future non-ban interaction sharing this Request
    URL -- cannot be answered with a 503 naming two environment variables.
    The refusal itself is unchanged: same status, same detail string, and
    raised at the same two moments in the flow (the button click and the modal
    submission), so an unwired deploy behaves exactly as it did before the
    dependency became optional.
    """
    if ban_client is None:
        raise HTTPException(status_code=503, detail=BAN_ADMIN_UNCONFIGURED_DETAIL)
    return ban_client


async def _handle_block_actions(
    payload: dict[str, Any],
    slack: SlackService | None,
    settings: Settings,
    ban_client: BanAdminClient | None,
    moderator_client: ModeratorClient | None,
) -> Response:
    actions = payload.get("actions") or []
    if not any(a.get("action_id") == BAN_BUTTON_ACTION_ID for a in actions):
        return Response(status_code=200)

    # Refuse at click time, not at submit time. Opening a reason modal that
    # cannot possibly save is a worse failure than the 503, and this is the
    # moment the raising dependency used to fire.
    _require_ban_client(ban_client)

    channel = _field(payload, "channel", "id")
    clicking_user = _field(payload, "user", "id")
    message = payload.get("message") or {}

    # Authorize before opening the modal, not only on submission. The ban
    # itself is already gated at view_submission, so this is not a bypass --
    # but the modal carries the listener's fingerprint in private_metadata,
    # which Slack hands to the opening client. Without this check any
    # workspace member who can see a request post can read a listener's
    # device UUID out of the view payload, in a repo that otherwise truncates
    # fingerprints in logs precisely to avoid that. Checked before
    # _extract_fingerprint so an unauthorized click never reads the value.
    authorized = await resolve_authorized_users(
        moderator_client, settings.slack_ban_authorized_users
    )
    if not is_authorized_slack_user_in(clicking_user, authorized.users):
        logger.warning(
            "slack_interactivity: unauthorized ban-button click user_id=%s", clicking_user
        )
        if slack is not None and channel and clicking_user:
            await _notify_slack(
                slack.post_ephemeral(
                    channel=channel,
                    user=clicking_user,
                    text="You are not authorized to ban requesters.",
                ),
                description="unauthorized-click ephemeral",
            )
        return Response(status_code=200)

    fingerprint = _extract_fingerprint(message)

    if fingerprint is None:
        # Reachable in production before the SLACK_USE_BOT_TOKEN cutover: the
        # button itself doesn't know which transport posted its message, so
        # it renders whenever a fingerprint was *attempted* -- but the
        # incoming-webhook transport drops chat.postMessage metadata entirely
        # (#209), so a webhook-posted message's button is a click with no
        # fingerprint behind it. Tell the clicker instead of silently
        # no-op'ing, which is indistinguishable from an outage mid-incident.
        logger.warning(
            "slack_interactivity: ban button clicked but message carries no "
            "usable fingerprint metadata; ignoring"
        )
        if slack is not None and channel and clicking_user:
            await _notify_slack(
                slack.post_ephemeral(
                    channel=channel,
                    user=clicking_user,
                    text=(
                        "This post has no device info attached to ban -- it may "
                        "have been sent before the fingerprint pipeline was "
                        "live for this deployment. Use the PostHog fallback in "
                        "docs/admin-bans.md instead."
                    ),
                ),
                description="no-fingerprint ephemeral notice",
            )
        return Response(status_code=200)

    if slack is None:
        logger.error(
            "slack_interactivity: ban button clicked but SLACK_BOT_TOKEN is "
            "unconfigured; cannot open a modal"
        )
        return Response(status_code=200)

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

    await _notify_slack(
        slack.open_view(trigger_id=trigger_id, view=_build_ban_reason_modal(private_metadata)),
        description=f"views.open fingerprint={_redact_fingerprint(fingerprint)}",
    )

    return Response(status_code=200)


async def _handle_view_submission(
    payload: dict[str, Any],
    slack: SlackService | None,
    ban_client: BanAdminClient | None,
    settings: Settings,
    moderator_client: ModeratorClient | None,
) -> Response:
    """Dispatch a modal submission by ``callback_id``.

    Was an early-return guard when the ban modal was the only one; a second
    modal turns it into a dispatch. **An unrecognized callback_id still returns
    200 and does nothing** -- that is not leniency, it is the contract: Slack
    delivers every modal submission across the whole app to this single
    Request URL, so an unknown id is a routine event rather than an error.
    """
    view = payload.get("view") or {}
    callback_id = view.get("callback_id")

    if callback_id == BAN_MODAL_CALLBACK_ID:
        return await _handle_ban_view_submission(
            payload, slack, ban_client, settings, moderator_client
        )
    if callback_id == MODERATOR_MODAL_CALLBACK_ID:
        return await _handle_moderator_view_submission(payload, settings, moderator_client)

    return Response(status_code=200)


def _roster_error(message: str) -> JSONResponse:
    """Keep the roster modal open and show ``message`` under the picker.

    Every refusal on this branch uses it. A bare 200 would close the modal and
    look exactly like a successful save -- which is the failure mode the whole
    optimistic-concurrency design exists to avoid, so it must not be the way
    refusals are rendered.
    """
    return JSONResponse(
        content={"response_action": "errors", "errors": {MODERATOR_BLOCK_ID: message}}
    )


async def _handle_moderator_view_submission(
    payload: dict[str, Any],
    settings: Settings,
    moderator_client: ModeratorClient | None,
) -> Response:
    """Save the moderator roster submitted from the /request-mods modal (#240).

    Takes no ``SlackService``: every outcome is rendered through this
    response's own body, so unlike the ban branch there is nothing here that
    needs the Web API and nothing that goes silent without ``SLACK_BOT_TOKEN``.
    """
    user_id = (payload.get("user") or {}).get("id")
    if not isinstance(user_id, str):
        logger.error("slack_interactivity: moderator view_submission missing user.id")
        return _roster_error("Could not identify the submitting user. Try again.")

    # Re-authorize. The submission is a separate HTTP request from the one that
    # opened the modal, so the authorization done there does not carry over --
    # a user whose access was revoked in between must not be able to save a
    # modal they already had open.
    authorized = await resolve_authorized_users(
        moderator_client, settings.slack_ban_authorized_users
    )
    if not is_authorized_slack_user_in(user_id, authorized.users):
        if authorized.degraded:
            # Do NOT call this a permissions decision. The roster half is
            # missing, so this user may well be a moderator we simply could not
            # confirm -- telling them they are unauthorized sends them to argue
            # about their access during an outage that has nothing to do with
            # them. The command that opens this modal already draws the same
            # distinction; the save path was the half that did not.
            logger.warning(
                "slack_interactivity: moderator-roster save could not be authorized "
                "(roster degraded) user=%s",
                user_id,
            )
            return _roster_error(
                "Couldn't reach the moderator list just now, so this couldn't be "
                "saved. Nothing was changed -- try again in a moment."
            )
        logger.warning(
            "slack_interactivity: unauthorized moderator-roster save by user=%s", user_id
        )
        sentry_sdk.add_breadcrumb(
            # Not "slack_ban": category is what Sentry indexes and groups by,
            # and it cannot be re-bucketed retroactively. Roster edits and bans
            # are different events with different operators and blast radii.
            category="slack_moderators",
            level="warning",
            message="Unauthorized moderator-roster save via Slack interactivity",
            data={"user_id": user_id},
        )
        return _roster_error("You are not authorized to manage moderators.")

    # Unreachable in practice -- resolve_authorized_users above would have
    # authorized off the environment allowlist alone, and the command that opens
    # this modal refuses to do so without a client. Checked anyway because the
    # alternative is an AttributeError on None, and because "the modal cannot
    # exist without a client" is an invariant held in another module.
    if moderator_client is None:
        logger.error("slack_interactivity: moderator roster save with no upstream configured")
        return _roster_error(
            "Moderator management is not configured. Set BS_INTERNAL_MODERATORS_URL."
        )

    view = payload.get("view") or {}
    selected = _nested_dict(view, "state", "values", MODERATOR_BLOCK_ID, MODERATOR_ACTION_ID).get(
        "selected_users"
    )
    # json.loads output is Any, and a non-list (or a list of non-strings) would
    # reach .upper() in the client and 500. Same fail-closed-on-shape posture as
    # is_authorized_slack_user_in -- and applied at every level of the walk
    # rather than only the leaf, which is where it used to stop.
    if not isinstance(selected, list) or not all(isinstance(uid, str) and uid for uid in selected):
        logger.error("slack_interactivity: moderator view_submission has malformed selected_users")
        return _roster_error("Could not read the selected users. Close this and try again.")

    try:
        # TypeError as well as JSONDecodeError: private_metadata is Any, and
        # json.loads of a dict/list/int raises TypeError, which the narrower
        # except does not catch.
        context = json.loads(view.get("private_metadata") or "{}")
    except (json.JSONDecodeError, TypeError):
        logger.error("slack_interactivity: malformed private_metadata on moderator submission")
        return _roster_error("This form is stale. Close it and run /request-mods again.")

    expected = context.get("moderators") if isinstance(context, dict) else None
    # ``and uid`` matches the selected_users guard above: an empty string can
    # never equal a real Slack ID, so shipping one as expectedCurrent guarantees
    # a 409 that blames a phantom concurrent editor.
    if not isinstance(expected, list) or not all(isinstance(uid, str) and uid for uid in expected):
        logger.error("slack_interactivity: moderator private_metadata missing a usable roster")
        return _roster_error("This form is stale. Close it and run /request-mods again.")

    try:
        saved = await moderator_client.replace_moderators(
            slack_user_ids=selected,
            expected_current=expected,
            actor_slack_user_id=user_id,
        )
    except ModeratorClientError as exc:
        if exc.status_code == 409:
            # Someone else saved between this modal opening and being submitted.
            # Surfacing it is the entire point: last-write-wins would silently
            # discard one of the two edits with nothing anywhere to show for it.
            current = exc.body.get("current") if isinstance(exc.body, dict) else None
            logger.warning(
                "slack_interactivity: moderator roster save conflicted (409) current=%r", current
            )
            return _roster_error(
                "Someone else changed the moderator list while this was open. "
                "Close this and run /request-mods again to see the current list."
            )
        logger.warning(
            "slack_interactivity: moderator roster save failed status=%d body=%r",
            exc.status_code,
            exc.body,
        )
        return _roster_error("Could not save the moderator list. Try again in a moment.")

    logger.info(
        "slack_interactivity: user=%s saved moderator roster (%d members)", user_id, len(saved)
    )
    return Response(status_code=200)


async def _handle_ban_view_submission(
    payload: dict[str, Any],
    slack: SlackService | None,
    ban_client: BanAdminClient | None,
    settings: Settings,
    moderator_client: ModeratorClient | None,
) -> Response:
    # Reached only for the ban modal's callback_id, so a submission for some
    # other modal sharing this Request URL is still the 200 no-op it has always
    # been rather than inheriting the ban flow's configuration requirements.
    ban_client = _require_ban_client(ban_client)

    view = payload.get("view") or {}
    user_id = (payload.get("user") or {}).get("id")
    if not isinstance(user_id, str):
        logger.error("slack_interactivity: view_submission payload missing user.id")
        return Response(status_code=200)

    try:
        context = json.loads(view.get("private_metadata") or "{}")
    except json.JSONDecodeError:
        logger.error("slack_interactivity: malformed private_metadata on view_submission")
        return Response(status_code=200)

    # Re-normalize on the read side. The value can only be one rom itself wrote
    # (build_slack_metadata writes the normalized value, and private_metadata
    # round-trips through a signature-verified payload), so this is belt and
    # braces -- but it makes the "only a well-formed UUID reaches /admin/bans"
    # guarantee local to this function instead of an invariant held three
    # modules away, and it is what the module docstring already implies.
    # isinstance first: private_metadata is json.loads output, so this is Any,
    # and normalize_fingerprint's .strip() would raise on a list -- turning a
    # malformed value into a 500 rather than the clean skip below.
    raw_fingerprint = context.get("fingerprint")
    fingerprint = (
        normalize_fingerprint(raw_fingerprint) if isinstance(raw_fingerprint, str) else None
    )
    channel = context.get("channel")
    message_ts = context.get("message_ts")
    original_blocks = context.get("blocks")

    authorized = await resolve_authorized_users(
        moderator_client, settings.slack_ban_authorized_users
    )
    if not is_authorized_slack_user_in(user_id, authorized.users):
        logger.warning("slack_interactivity: unauthorized ban attempt by user=%s", user_id)
        sentry_sdk.add_breadcrumb(
            category="slack_ban",
            level="warning",
            message="Unauthorized ban attempt via Slack interactivity",
            data={"user_id": user_id},
        )
        if slack is not None and channel:
            await _notify_slack(
                slack.post_ephemeral(
                    channel=channel,
                    user=user_id,
                    text="You are not authorized to ban requesters.",
                ),
                description="unauthorized-refusal ephemeral",
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
            await _notify_slack(
                slack.post_ephemeral(
                    channel=channel,
                    user=user_id,
                    text="Ban failed -- Backend-Service rejected the request. Check the logs.",
                ),
                description=f"ban-failure ephemeral fingerprint={_redact_fingerprint(fingerprint)}",
            )
        return Response(status_code=200)

    # No stashed blocks means the original post was too long for
    # private_metadata (see _MAX_PRIVATE_METADATA_LEN). Editing anyway would
    # replace the post with the footer alone, so leave the post untouched and
    # say so in the ack rather than annotating it destructively.
    can_annotate = isinstance(original_blocks, list) and bool(original_blocks)

    if slack is not None:
        # Both notifications are best-effort: the ban already landed in BS by
        # this point, so neither a failed ack nor a failed edit should turn
        # into a 500 that tells Slack (and the DJ) the action failed.
        ack = f"Banned. Reason: {reason}"
        if not can_annotate:
            ack += " (The original post was too long to annotate, so it was left as-is.)"
        await _notify_slack(
            slack.post_ephemeral(channel=channel, user=user_id, text=ack),
            description=f"ban-success ephemeral fingerprint={_redact_fingerprint(fingerprint)}",
        )
        if can_annotate:
            await _notify_slack(
                slack.update_message(
                    channel=channel,
                    ts=message_ts,
                    blocks=_build_updated_blocks(original_blocks, user_id, reason),
                ),
                description=f"chat.update fingerprint={_redact_fingerprint(fingerprint)}",
            )
        else:
            logger.warning(
                "slack_interactivity: skipping chat.update for fingerprint=%s -- no stashed "
                "blocks, editing would erase the original post",
                _redact_fingerprint(fingerprint),
            )

    return Response(status_code=200)


@router.post(
    "/interactivity",
    dependencies=[Depends(verify_slack_request)],
    summary="Slack interactivity callback (button clicks + modal submissions)",
    description=(
        "The single Request URL for this Slack app's interactive components. "
        "Handles the 'Ban requester' button (#152) -- a block_actions click "
        "opens a reason modal, and the resulting view_submission bans the "
        "fingerprint carried in the original message's metadata -- and the "
        "/request-mods moderator roster save (#240), whose modal is opened by "
        "POST /slack/commands but submitted here. Any other callback_id is an "
        "acknowledged no-op."
    ),
    responses={
        200: {"description": "Acknowledged (always, regardless of internal handling)"},
        400: {"description": "Missing or malformed interaction payload"},
        401: {"description": "Invalid or missing Slack request signature"},
        503: {
            "description": (
                "Ban upstream not configured. Raised only from the ban paths, "
                "not from the route's dependencies -- interactions that do not "
                "need a ban client are unaffected."
            )
        },
    },
)
async def slack_interactivity(
    request: Request,
    settings: Settings = Depends(get_settings),
    slack: SlackService | None = Depends(get_slack_interactivity_service),
    # Optional, NOT the raising variant: handler-parameter dependencies resolve
    # before the interaction-type dispatch below, so the raising one answers
    # every callback reaching this shared Request URL -- ban-related or not --
    # with a 503 naming two ban-only env vars. The refusal moves into the ban
    # paths via _require_ban_client. See get_optional_ban_admin_client.
    ban_client: BanAdminClient | None = Depends(get_optional_ban_admin_client),
    moderator_client: ModeratorClient | None = Depends(get_moderator_client),
) -> Response:
    """POST /slack/interactivity -- dispatch by interaction type.

    Signature verification already happened in ``verify_slack_request``, the
    route-level dependency: nothing below here runs for an unsigned request.
    """
    raw_body = await request.body()

    # errors="replace" rather than a raise: a non-UTF-8 body is only reachable
    # behind a valid signature, but letting it raise UnicodeDecodeError turns a
    # malformed payload into a 500, and an unhandled 500 ships this frame's
    # locals to Sentry. A body that decodes to replacement characters simply
    # fails the parse below and gets a 400 like every other malformed payload.
    form = parse_qs(raw_body.decode("utf-8", errors="replace"))
    payload_values = form.get("payload")
    if not payload_values:
        raise HTTPException(status_code=400, detail="Missing payload")

    try:
        payload = json.loads(payload_values[0])
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Malformed payload") from exc

    # Valid JSON that isn't an object (a list, a bare string, null) would reach
    # payload.get() and raise AttributeError -- another 500 where the two
    # neighbouring malformed cases already return 400.
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Malformed payload")

    interaction_type = payload.get("type")
    if interaction_type == "block_actions":
        return await _handle_block_actions(payload, slack, settings, ban_client, moderator_client)
    if interaction_type == "view_submission":
        return await _handle_view_submission(payload, slack, ban_client, settings, moderator_client)

    logger.info("slack_interactivity: ignoring unhandled interaction type=%s", interaction_type)
    return Response(status_code=200)
