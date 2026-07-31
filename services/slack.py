import logging
from typing import Any

from generated.api_models import LibraryLocation
from models import LibraryItem, ReleaseMetadata, preview_url

logger = logging.getLogger(__name__)

# Per-release permalink template (LML#1014 / dj-site#1050). LibraryLocation carries
# only a library_id (the union is deliberately lean — no live URL resolution), so we
# rebuild the same dj-site legacy front door LML emits for LibraryItem.library_url.
_LIBRARY_PERMALINK_BASE = "https://dj.wxyc.org/dashboard/album/legacy"

# Cap the alternate-locations section so a track carried on many compilations can't
# overflow Slack's 3000-char section-text limit. Overflow is summarized as "…and N more".
_MAX_ALSO_AVAILABLE_LOCATIONS = 10


def _also_available_block(locations: list[LibraryLocation]) -> dict:
    """Render the ranked alternate shelf locations as one Slack section block.

    LML ranks ``also_available_on``; we preserve that order and link each entry to
    its dj.wxyc.org shelf permalink so a DJ can pull a backup copy.
    """
    lines = ["*Also available on:*"]
    for loc in locations[:_MAX_ALSO_AVAILABLE_LOCATIONS]:
        title = loc.album_title or loc.track_title or "Unknown release"
        url = f"{_LIBRARY_PERMALINK_BASE}/{loc.library_id}"
        parts = [f"<{url}|{title}>"]
        if loc.artist:
            parts.append(f"— {loc.artist}")
        if loc.track_position:
            parts.append(f"({loc.track_position})")
        lines.append("• " + " ".join(parts))
    remaining = len(locations) - _MAX_ALSO_AVAILABLE_LOCATIONS
    if remaining > 0:
        lines.append(f"_…and {remaining} more_")
    return {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}


def build_slack_blocks(
    message: str,
    items_with_artwork: list[tuple[LibraryItem, ReleaseMetadata | None]],
    context: str | None = None,
    also_available_on: list[LibraryLocation] | None = None,
) -> list[dict]:
    """Build Slack message blocks from library results with artwork.

    When ``also_available_on`` is non-empty, a trailing "Also available on" section
    lists the other WXYC shelf locations carrying the same track (LML#1018/#1022).
    Absent or empty, the output is byte-identical to before.
    """
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

    if also_available_on:
        blocks.append(_also_available_block(also_available_on))

    return blocks


def build_simple_slack_blocks(message: str, context: str | None = None) -> list[dict[str, Any]]:
    """Build simple Slack message blocks for feedback or no-results messages."""
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{message}*"}}
    ]

    if context:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": context}]})

    return blocks
