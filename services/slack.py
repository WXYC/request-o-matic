import logging
from typing import Any

from models import LibraryItem, ReleaseMetadata, preview_url

logger = logging.getLogger(__name__)


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
