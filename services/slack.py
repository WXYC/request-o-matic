import logging
from typing import Any

import httpx

from config.settings import get_settings
from discogs.models import DiscogsSearchResult
from library.models import LibraryItem

logger = logging.getLogger(__name__)

_webhook_url: str | None = None
_http_client: httpx.AsyncClient | None = None


async def init_slack_service():
    """Initialize Slack service with webhook URL."""
    global _webhook_url, _http_client

    _http_client = httpx.AsyncClient(timeout=30.0)

    # Check for webhook URL override, otherwise fetch from configured URL
    settings = get_settings()
    if settings.slack_webhook_url:
        _webhook_url = settings.slack_webhook_url
        logger.info("Using Slack webhook URL from environment")
    else:
        try:
            response = await _http_client.get(settings.slack_webhook_key_url)
            response.raise_for_status()
            webhook_key = response.text.strip()
            _webhook_url = f"https://hooks.slack.com/services/{webhook_key}"
            logger.info(f"Slack webhook URL configured from {settings.slack_webhook_key_url}")
        except Exception as e:
            logger.error(f"Failed to fetch Slack webhook key: {e}")
            raise RuntimeError(f"Failed to fetch Slack webhook key: {e}") from e


async def shutdown_slack_service():
    """Clean up Slack service resources."""
    global _http_client, _webhook_url
    if _http_client:
        await _http_client.aclose()
        _http_client = None
    _webhook_url = None
    logger.info("Slack service shut down")


def build_slack_blocks(
    message: str,
    items_with_artwork: list[tuple[LibraryItem, DiscogsSearchResult | None]],
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
            text_lines.append(f"<{artwork.release_url}|Discogs> | <{item.library_url}|WXYC>")

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


async def post_to_slack(blocks: list[dict]) -> None:
    """Post message blocks to Slack webhook."""
    if not _webhook_url or not _http_client:
        raise RuntimeError("Slack webhook not configured")

    response = await _http_client.post(_webhook_url, json={"blocks": blocks})
    response.raise_for_status()
    logger.info("Posted to Slack successfully")
