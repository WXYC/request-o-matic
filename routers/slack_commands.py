"""Slack slash-command endpoint for managing the ban-moderator roster (#240).

``POST /slack/commands`` handles ``/request-mods``: it opens a modal with the
current moderators pre-selected in a ``multi_users_select``, and the resulting
``view_submission`` is saved by ``routers/slack_interactivity.py`` (Slack sends
every modal submission in the app to that single interactivity Request URL,
which is why the save lives there and not here).

Signature verification runs as a route-level ``dependencies=[...]`` entry, not
as a handler parameter -- see :func:`core.dependencies.verify_slack_request`
for why that distinction is load-bearing rather than stylistic.

Three properties worth knowing before editing this file:

**One upstream read, not two.** The roster fetched to authorize the invoker is
the same roster that populates ``initial_users``. Slack invalidates
``trigger_id`` after ~3 seconds, so the budget between receiving the command
and calling ``views.open`` is one Backend-Service round-trip and no more. That
is also why this does not call ``resolve_authorized_users`` -- it would issue
its own read.

**Every refusal is a 200 with an ephemeral JSON body**, never a
``chat.postEphemeral`` call. The interactivity router's refusals go through the
Web API and are guarded by ``if slack is not None``, because that dependency
returns None without ``SLACK_BOT_TOKEN``; reusing that shape here would make a
bot-token-less deploy refuse *silently*. A slash command doesn't need the Web
API to say no -- the response body reaches the invoker unconditionally.

**Refusing beats degrading.** Unlike the ban button, which falls back to the
environment allowlist when Backend-Service is unreachable, ``/request-mods``
refuses outright. There is no useful degraded modal: one populated from a
failed read would show an empty picker, and saving that would wipe the roster.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from config.settings import Settings, get_settings
from core.dependencies import (
    SLACK_VIEW_OPEN_BUDGET_SECONDS,
    SlackService,
    get_moderator_client,
    get_slack_interactivity_service,
    verify_slack_request,
)
from core.exceptions import SlackPostError
from routers.slack_interactivity import (
    MODERATOR_ACTION_ID,
    MODERATOR_BLOCK_ID,
    MODERATOR_MODAL_CALLBACK_ID,
)
from services.moderator_client import ModeratorClient, ModeratorClientError
from services.slack import MAX_PRIVATE_METADATA_LEN
from services.slack_authorization import is_authorized_slack_user_in, parse_authorized_users

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/slack", tags=["slack-commands"])

MODS_COMMAND = "/request-mods"

#: What an unauthorized caller is told when the deployment is misconfigured.
#: Deliberately says nothing about *which* variable is unset, or whether the
#: upstream is unreachable rather than absent -- a workspace member who cannot
#: manage moderators has no business learning the deployment's configuration.
_UNAVAILABLE = "Moderator management is unavailable right now."

#: Slack's response deadline for a slash command. Past it, Slack shows the
#: invoker its own timeout error and DISCARDS whatever this route returns --
#: which would silently drop the ephemeral refusals the design depends on.
#: Used to decide whether a `views.open` retry still has room to be useful.
SLASH_COMMAND_BUDGET_SECONDS = 3.0

# Slack's cap on a multi_users_select's initial_users, and the same bound BS
# enforces on the roster. Named here so the private_metadata arithmetic below
# is checkable rather than asserted.
MAX_ROSTER_SIZE = 100

# views.open errors worth retrying without initial_users. Deliberately narrow:
# `expired_trigger_id` is guaranteed to fail again, and a timeout would spend a
# second full budget on a window that has already closed. Slack reports a bad
# initial_users entry as `invalid_arguments`.
_RETRYABLE_VIEW_OPEN_ERRORS = ("invalid_arguments",)


def _ephemeral(text: str) -> JSONResponse:
    """A refusal only the invoker sees, delivered in the response body.

    Requires no bot token and no Web API call, which is the entire point --
    see the module docstring.
    """
    return JSONResponse(content={"response_type": "ephemeral", "text": text})


def _form_field(form: dict[str, list[str]], name: str) -> str:
    values = form.get(name)
    return values[0] if values else ""


def _is_initial_users_rejection(exc: SlackPostError) -> bool:
    message = str(exc)
    return any(code in message for code in _RETRYABLE_VIEW_OPEN_ERRORS)


def _build_roster_modal(
    *,
    moderators: list[str],
    break_glass: frozenset[str],
    dropped: list[str] | None = None,
) -> dict[str, Any]:
    """Build the /request-mods modal.

    Args:
        moderators: The stored roster, used for ``initial_users`` and
            round-tripped through ``private_metadata`` as ``expectedCurrent``.
        break_glass: Environment-allowlist members, rendered read-only.
        dropped: When set, ``initial_users`` is omitted and these IDs are named
            instead -- the retry path for a roster Slack refused to pre-select.
    """
    picker: dict[str, Any] = {
        "type": "multi_users_select",
        "action_id": MODERATOR_ACTION_ID,
        "placeholder": {"type": "plain_text", "text": "Choose moderators"},
    }
    # Omitted when empty: Slack rejects an empty initial_users array, and an
    # empty roster is the normal day-one state rather than an edge case.
    if moderators and dropped is None:
        picker["initial_users"] = moderators

    blocks: list[dict[str, Any]] = [
        {
            "type": "input",
            "block_id": MODERATOR_BLOCK_ID,
            # Without this, Slack refuses to submit an empty selection --
            # making "remove the last moderator" impossible from the only UI
            # that can remove moderators.
            "optional": True,
            "label": {"type": "plain_text", "text": "Who can ban request-line abusers?"},
            "element": picker,
        }
    ]

    if dropped:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            "Could not pre-select the current list (one of these accounts may "
                            "be deactivated): "
                            + ", ".join(f"`{user_id}`" for user_id in dropped)
                            + ". Re-select everyone who should stay before saving."
                        ),
                    }
                ],
            }
        )

    # The picker shows the table, but the table is not what authorizes. Without
    # this block the modal understates who can ban -- most starkly on day one,
    # when the table is empty and the environment allowlist is doing all the
    # work, and again after the break-glass trim, when deselecting an
    # environment member would appear to work and change nothing.
    if break_glass:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            "Also authorized via break-glass, not editable here: "
                            + ", ".join(f"<@{user_id}>" for user_id in sorted(break_glass))
                            + " (edit `SLACK_BAN_AUTHORIZED_USERS` in Railway)."
                        ),
                    }
                ],
            }
        )

    # ``initial_users_dropped`` marks a retry view, whose picker opens EMPTY
    # because Slack rejected the pre-selection. Without the marker, hitting Save
    # on that view sends selected_users=[] with an expectedCurrent that still
    # matches the stored roster -- so no 409 fires and Backend-Service empties
    # the table. On the normal path an empty save requires deliberately
    # deselecting everyone; on the retry path empty is the DEFAULT state, and it
    # is reached precisely when the roster is already in trouble. The save
    # branch refuses an empty selection from a marked view.
    context: dict[str, Any] = {"moderators": moderators}
    if dropped:
        context["initial_users_dropped"] = True
    private_metadata = json.dumps(context)
    if len(private_metadata) > MAX_PRIVATE_METADATA_LEN:
        # Unreachable at the 100-ID cap -- a JSON array of 11-character IDs runs
        # about 1,400 characters, under half the budget. Written anyway so that
        # if the cap ever rises this is caught rather than rediscovered as a
        # Slack error. Falling back to an empty expectedCurrent would be worse
        # than refusing: BS would read it as "the roster was empty when I
        # looked" and the save would clobber whatever is actually stored.
        logger.error(
            "slack_commands: roster private_metadata is %d chars, over the %d cap",
            len(private_metadata),
            MAX_PRIVATE_METADATA_LEN,
        )
        raise ValueError("roster private_metadata exceeds Slack's cap")

    return {
        "type": "modal",
        "callback_id": MODERATOR_MODAL_CALLBACK_ID,
        "title": {"type": "plain_text", "text": "Request-line mods"},
        "submit": {"type": "plain_text", "text": "Save"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "private_metadata": private_metadata,
        "blocks": blocks,
    }


@router.post(
    "/commands",
    dependencies=[Depends(verify_slack_request)],
    summary="Slack slash commands",
    description=(
        "Handles /request-mods (#240): opens a modal for editing the ban-moderator "
        "roster stored in Backend-Service. The resulting view_submission is handled "
        "by POST /slack/interactivity, Slack's single interactivity Request URL."
    ),
    responses={
        200: {"description": "Acknowledged, or an ephemeral refusal"},
        401: {"description": "Invalid or missing Slack request signature"},
    },
)
async def slack_commands(
    request: Request,
    settings: Settings = Depends(get_settings),
    slack: SlackService | None = Depends(get_slack_interactivity_service),
    moderator_client: ModeratorClient | None = Depends(get_moderator_client),
) -> Response:
    """POST /slack/commands -- dispatch by ``command``.

    Signature verification already happened in ``verify_slack_request``, the
    route-level dependency: nothing below here runs for an unsigned request.
    """
    started_at = time.monotonic()
    raw_body = await request.body()
    # errors="replace" for the same reason as the interactivity router: a
    # non-UTF-8 body is only reachable behind a valid signature, but letting it
    # raise turns a malformed payload into a 500 whose Sentry event carries
    # this frame's locals.
    form = parse_qs(raw_body.decode("utf-8", errors="replace"))

    command = _form_field(form, "command")
    if command != MODS_COMMAND:
        # Not an error: one Request URL can serve several commands, so an
        # unrecognized one is a routine event.
        logger.info("slack_commands: ignoring unhandled command=%s", command)
        return _ephemeral("Unknown command.")

    user_id = _form_field(form, "user_id")
    trigger_id = _form_field(form, "trigger_id")

    if not trigger_id:
        logger.error("slack_commands: %s invoked with no trigger_id", MODS_COMMAND)
        return _ephemeral("Something went wrong opening the moderator list. Try again.")

    break_glass = parse_authorized_users(settings.slack_ban_authorized_users)

    # Resolved before the two refusals below, because both name an environment
    # variable and describe the deployment's configuration state. Without this,
    # any workspace member could type /request-mods and learn them -- the same
    # disclosure class the signature-ordering tests exist to prevent, just one
    # step further in (authenticated but unauthorized rather than anonymous).
    # It is a set-membership test against an already-parsed CSV, so it costs no
    # upstream call and does not touch the trigger_id budget.
    def _configuration_detail(message: str, fallback: str) -> JSONResponse:
        if is_authorized_slack_user_in(user_id, break_glass):
            return _ephemeral(message)
        return _ephemeral(fallback)

    if moderator_client is None:
        logger.warning(
            "slack_commands: %s invoked with no roster upstream configured", MODS_COMMAND
        )
        return _configuration_detail(
            "Moderator management isn't set up on this deployment "
            "(`BS_INTERNAL_MODERATORS_URL` is unset). Bans still work off the "
            "break-glass allowlist.",
            _UNAVAILABLE,
        )

    # The single read. Authorization and initial_users both come from it.
    try:
        moderators = await moderator_client.list_moderators()
    except ModeratorClientError as exc:
        logger.warning("slack_commands: could not read the moderator roster (%s)", exc)
        return _configuration_detail(
            "Couldn't reach the moderator list just now. Try again in a moment "
            "-- banning still works.",
            _UNAVAILABLE,
        )

    authorized = break_glass | frozenset(moderators)

    if not is_authorized_slack_user_in(user_id, authorized):
        # Refuse without naming anyone. The roster isn't secret, but it
        # shouldn't be casually enumerable by the whole workspace -- the same
        # posture as the ban button's pre-modal check.
        logger.warning("slack_commands: unauthorized %s by user=%s", MODS_COMMAND, user_id)
        return _ephemeral("You are not authorized to manage request-line moderators.")

    if slack is None:
        logger.error("slack_commands: %s needs SLACK_BOT_TOKEN to open a modal", MODS_COMMAND)
        return _ephemeral(
            "Moderator management isn't set up on this deployment (`SLACK_BOT_TOKEN` is unset)."
        )
        # Reached only past the authorization gate above, so naming the variable
        # here is fine -- the caller is already a moderator.

    try:
        view = _build_roster_modal(moderators=moderators, break_glass=break_glass)
    except ValueError:
        return _ephemeral(
            "The moderator list is too large to edit from Slack. Trim it in Backend-Service first."
        )

    try:
        await slack.open_view(trigger_id=trigger_id, view=view)
    except SlackPostError as exc:
        # Only payload rejections are worth a second attempt, and only by
        # dropping initial_users -- a deactivated account accumulating in the
        # roster must not make the roster uneditable by the one tool that can
        # remove it. Timeouts and transport errors are httpx exceptions, not
        # SlackPostError, so they never reach here and never consume the retry.
        if not _is_initial_users_rejection(exc):
            logger.warning("slack_commands: views.open failed, not retrying (%s)", exc)
            return _ephemeral("Couldn't open the moderator list. Try again.")

        # Slack discards the response body once the 3s slash-command deadline
        # passes and renders its own timeout instead -- so a retry that runs
        # past it costs the invoker the ephemeral this route is architected
        # around, in exactly the degraded case the ephemeral exists for.
        # Spending the remaining budget is only worth it if there is enough of
        # it left to also deliver the answer.
        remaining = SLASH_COMMAND_BUDGET_SECONDS - (time.monotonic() - started_at)
        if remaining < SLACK_VIEW_OPEN_BUDGET_SECONDS:
            logger.warning(
                "slack_commands: skipping the initial_users retry, %.2fs left of the "
                "slash-command budget",
                remaining,
            )
            return _ephemeral(
                "Couldn't open the moderator list -- Slack rejected the current "
                "selection and there wasn't time to retry. Run /request-mods again."
            )

        logger.warning(
            "slack_commands: views.open rejected initial_users (%s); retrying without it", exc
        )
        try:
            retry_view = _build_roster_modal(
                moderators=moderators, break_glass=break_glass, dropped=moderators
            )
            await slack.open_view(trigger_id=trigger_id, view=retry_view)
        except Exception as retry_exc:
            # Catches everything, NOT just (SlackPostError, ValueError). The
            # sibling `except Exception` below cannot cover this: a handler
            # never catches what another handler on the same `try` raises, so
            # a timeout on the retry would escape as a 500 -- and this retry
            # only runs when Slack is already misbehaving, which is precisely
            # when a timeout is likely rather than exotic.
            logger.warning(
                "slack_commands: views.open retry failed (%s: %s)",
                type(retry_exc).__name__,
                retry_exc,
            )
            return _ephemeral("Couldn't open the moderator list. Try again.")
    except Exception as exc:
        # Timeout or transport failure. Bounded by SLACK_VIEW_OPEN_TIMEOUT_SECONDS
        # rather than the shared client's 30s, so this surfaces while the
        # invoker is still looking at Slack.
        logger.warning("slack_commands: views.open errored (%s: %s)", type(exc).__name__, exc)
        return _ephemeral("Couldn't open the moderator list. Try again.")

    logger.info(
        "slack_commands: opened the moderator picker for user=%s (%d stored)",
        user_id,
        len(moderators),
    )
    # An empty 200 body: Slack shows nothing in-channel, and the modal is
    # already open.
    # Response, not JSONResponse(content=None): the latter sends the four
    # bytes b"null" with a JSON content-type, which Slack may parse as a
    # command response and render to the invoker AFTER the modal opened.
    # This matches how the interactivity router acks.
    return Response(status_code=200)
