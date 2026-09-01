import logging
from typing import Any

from models import LibraryItem, ReleaseMetadata, preview_url
from services.fingerprint import normalize_fingerprint

logger = logging.getLogger(__name__)

# chat.postMessage metadata event_type carrying the requester's device
# fingerprint (request-o-matic#209). WXYC/request-o-matic#152's "Ban
# requester" button matches on this string to find the fingerprint in a
# message's metadata.event_payload -- do not rename without updating that
# consumer, or every existing button silently stops finding a fingerprint.
SLACK_METADATA_EVENT_TYPE = "request_posted"

# action_id on the "Ban requester" overflow menu (request-o-matic#152).
# routers/slack_interactivity.py matches on this to recognize the click --
# keep in sync with that consumer, same rationale as SLACK_METADATA_EVENT_TYPE
# above. Overflow actions carry ``action_id`` in the block_actions payload
# exactly as buttons do, so the handler's matching is unchanged.
BAN_BUTTON_ACTION_ID = "ban_requester"

# Slack requires a ``value`` on every overflow option. The button this replaced
# deliberately carried none, and that property has to survive the change: the
# fingerprint acted on comes from the clicked message's own metadata (see
# build_slack_metadata / SLACK_METADATA_EVENT_TYPE), read by the interactivity
# handler out of the signature-verified payload. This constant exists so the
# required field can be satisfied with something inert -- it is never read back,
# and nothing request-specific may ever be encoded here.
BAN_MENU_OPTION_VALUE = "ban_requester"

# Slack caps ``private_metadata`` at 3000 characters on any view payload.
#
# Two routers now stash state there -- the ban modal (the original consumer,
# which degrades by dropping the message blocks and skipping its chat.update
# footer when they don't fit) and the moderator modal (#240, which round-trips
# the roster it read so Backend-Service can detect a concurrent edit). It lives
# here, beside the other cross-router Slack constants, rather than staying
# module-private in one of them: a router importing a sibling router's
# underscored name is the coupling this design rejects everywhere else.
#
# The moderator modal's guard against this is unreachable in practice -- at
# BS's 100-ID cap, a JSON array of 11-character IDs runs about 1,400 characters,
# under half the budget -- and it is written anyway so that if the cap ever
# rises, the ceiling is enforced rather than rediscovered.
MAX_PRIVATE_METADATA_LEN = 3000


def _build_ban_menu_block() -> dict[str, Any]:
    """Build the overflow ("...") menu holding the ban action.

    An overflow rather than a standalone ``danger`` button (#237): a red button
    under every request post reads as an invitation to use it. That reduced the
    volume of the problem without changing its kind -- see
    :func:`maybe_append_ban_button` for why the menu no longer rides on the
    public post at all.
    """
    return {
        "type": "actions",
        "elements": [
            {
                "type": "overflow",
                "action_id": BAN_BUTTON_ACTION_ID,
                "options": [
                    {
                        "text": {"type": "plain_text", "text": "Ban requester"},
                        "value": BAN_MENU_OPTION_VALUE,
                    }
                ],
            }
        ],
    }


def build_slack_metadata(fingerprint: str | None) -> dict[str, Any] | None:
    """Build the chat.postMessage ``metadata`` envelope for a requester's
    device fingerprint (request-o-matic#209).

    Returns None when there is no *usable* fingerprint so callers can omit the
    ``metadata`` key entirely rather than sending an empty/null-valued one --
    WXYC/request-o-matic#152 keys the ban button's presence off that
    distinction. The fingerprint must never appear in the rendered blocks;
    this metadata envelope is the only carrier.

    "Usable" means "a UUID ``POST /admin/bans`` will accept", which is what
    ``normalize_fingerprint`` enforces: an empty (FastAPI binds a present-but-
    empty header to ``""``, not None), whitespace-only, or malformed value would
    otherwise render a ban button that 422s on every click.
    """
    normalized = normalize_fingerprint(fingerprint)
    if normalized is None:
        return None
    return {
        "event_type": SLACK_METADATA_EVENT_TYPE,
        "event_payload": {"fingerprint": normalized},
    }


def maybe_append_ban_button(
    blocks: list[dict[str, Any]], fingerprint: str | None
) -> list[dict[str, Any]]:
    """Append the "Ban requester" actions block iff ``fingerprint`` normalizes
    to a usable UUID (request-o-matic#152).

    **This has no production caller.** ``routers/request.py`` called it on both
    of its Slack-posting paths until the public post stopped carrying the menu;
    the next caller is the moderators-channel post, which is where the
    affordance is being re-homed. It is kept rather than deleted because
    nothing about *building* the block was wrong -- the defect was its
    audience.

    The audience is the whole point. ``chat.postMessage`` sends one payload to
    every member of a channel and Slack has no per-viewer block visibility, so
    a menu on a public request post is visible to every DJ and usable by the
    handful on the ban roster. Authorization is enforced at click time in
    ``routers/slack_interactivity.py`` and was never the gap; the gap was that
    a visible control reads as an available one, which is what a DJ asked about
    on 2026-08-31. Per-viewer visibility is only reachable by changing the
    delivery mechanism, not by conditioning this function on a user. **So do
    not add a ``user_id`` parameter here** -- there is no payload field it
    could drive.

    Mirrors ``build_slack_metadata``'s usable/unusable split exactly, via the
    same ``normalize_fingerprint`` call: a post without a usable fingerprint
    gets no menu, since there is nothing behind it to ban and an action that
    422s on every click is worse than no action at all. Returns a new list
    rather than mutating ``blocks`` in place, containing a freshly-built menu
    block rather than a shared reference.
    """
    if normalize_fingerprint(fingerprint) is None:
        return blocks
    return [*blocks, _build_ban_menu_block()]


def build_slack_blocks(
    message: str,
    items_with_artwork: list[tuple[LibraryItem, ReleaseMetadata | None]],
    context: str | None = None,
) -> list[dict]:
    """Build Slack message blocks from library results with artwork."""
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": f"*{message}*"}}]

    # Add context message if provided (e.g., "song not found, showing artist albums")
    if context:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": context}})

    for item, artwork in items_with_artwork:
        # Build text with links to library and Discogs
        text_lines = [
            f"*{item.artist or 'Unknown Artist'}*",
            f"{item.title or 'Unknown Title'}",
            f"_{item.call_number}_",
        ]
        if artwork and artwork.release_url:
            # A row-less external result (LML#631) has no WXYC catalog page —
            # it carries id=0 and an empty library_url. Omit the WXYC link in
            # that case rather than emitting a malformed empty-target <|WXYC>;
            # the "(external)" call number already marks it as not-in-library.
            link_parts = [f"<{artwork.release_url}|Discogs>"]
            if item.library_url:
                link_parts.append(f"<{item.library_url}|WXYC>")
            if (preview := preview_url(artwork)) is not None:
                link_parts.append(f"<{preview}|Preview>")
            text_lines.append(" | ".join(link_parts))

        block: dict = {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(text_lines)}}

        if artwork and artwork.artwork_url:
            block["accessory"] = {
                "type": "image",
                "image_url": artwork.artwork_url,
                "alt_text": f"{item.title} album cover",
            }

        blocks.append(block)

    return blocks


def build_simple_slack_blocks(message: str, context: str | None = None) -> list[dict[str, Any]]:
    """Build simple Slack message blocks for feedback or no-results messages."""
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{message}*"}}
    ]

    if context:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": context}]})

    return blocks
