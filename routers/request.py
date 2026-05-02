"""Request handling router that delegates search to library-metadata-lookup service.

Flow:
1. Parse the message using Groq AI to extract artist/song/album
2. Early return for non-requests (feedback, DJ messages, etc.)
3. Delegate search to library-metadata-lookup service via HTTP
4. Post enriched results to Slack

Degraded modes:
- "parsing_unavailable": Groq parsing failed. Slack receives the raw listener
  message with a "_Parsing unavailable_" note. No classification, no search.
- "search_unavailable": LML is down or unconfigured. Slack receives the parsed
  metadata with a "_Search unavailable_" note. No library results.

Slack remains the only hard dependency: if it fails, the endpoint returns 502.
"""

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException
from groq import AsyncGroq
from posthog import Posthog
from pydantic import BaseModel

from core.dependencies import (
    SlackService,
    get_groq_client,
    get_lookup_client,
    get_posthog_client,
    get_slack_service,
)
from core.telemetry import RequestTelemetry, get_cache_stats, init_cache_stats
from models import LibraryItem, ReleaseMetadata
from services.lookup_client import LookupRequest, LookupServiceClient
from services.parser import MessageType, ParsedRequest, parse_request
from services.slack import build_simple_slack_blocks, build_slack_blocks

logger = logging.getLogger(__name__)


# Friendly labels for message types in Slack
MESSAGE_TYPE_LABELS = {
    MessageType.REQUEST: "Song Request",
    MessageType.DJ_MESSAGE: "Message to DJ",
    MessageType.FEEDBACK: "Feedback",
    MessageType.OTHER: "Other",
}

# Degraded-mode identifiers surfaced in the response and telemetry.
DEGRADED_PARSING = "parsing_unavailable"
DEGRADED_SEARCH = "search_unavailable"

# httpx exceptions that count as an LML outage rather than a programming error.
_LML_TRANSIENT_ERRORS = (
    httpx.HTTPStatusError,
    httpx.ConnectError,
    httpx.TimeoutException,
)

router = APIRouter(tags=["request"])


class RequestBody(BaseModel):
    """Request body for song request parsing."""

    message: str
    skip_slack: bool = False
    skip_cache: bool = False


class UnifiedResponse(BaseModel):
    """Combined response from parsing, artwork lookup, and library search."""

    parsed: ParsedRequest | None = None
    artwork: ReleaseMetadata | None = None
    library_results: list[LibraryItem] = []
    # Search metadata
    search_type: str = "none"
    song_not_found: bool = False
    found_on_compilation: bool = False
    context_message: str | None = None
    cache_stats: dict | None = None
    # When set, indicates a degraded path. See DEGRADED_* constants.
    degraded_mode: str | None = None


def _parsed_context_parts(parsed: ParsedRequest) -> list[str]:
    """Build a human-readable summary of parsed fields for Slack context lines."""
    parts: list[str] = []
    if parsed.artist:
        parts.append(f"Artist: {parsed.artist}")
    if parsed.album:
        parts.append(f"Album: {parsed.album}")
    if parsed.song:
        parts.append(f"Song: {parsed.song}")
    return parts


async def post_results_to_slack(
    slack_service: SlackService | None,
    message: str,
    parsed: ParsedRequest,
    items_with_artwork: list[tuple[LibraryItem, ReleaseMetadata | None]],
    context: str | None = None,
) -> None:
    """Post formatted results to Slack.

    Args:
        slack_service: Slack service instance
        message: Original request message
        parsed: Parsed request
        items_with_artwork: Library items with their artwork
        context: Optional context message

    Raises:
        HTTPException: If posting to Slack fails
    """
    if not slack_service:
        logger.info("Slack integration disabled, skipping post")
        return

    if items_with_artwork:
        blocks = build_slack_blocks(message, items_with_artwork, context)
    elif not parsed.is_request:
        label = MESSAGE_TYPE_LABELS.get(parsed.message_type, "Other")
        blocks = build_simple_slack_blocks(message, f"_{label}_")
    else:
        # Request but no results found
        ctx = " | ".join(_parsed_context_parts(parsed)) or None
        blocks = build_simple_slack_blocks(message, f"_No results found_ {ctx or ''}")

    try:
        await slack_service.post_blocks(blocks)
    except Exception as e:
        logger.error(f"Failed to post to Slack: {e}")
        raise HTTPException(status_code=502, detail=f"Failed to post to Slack: {e}") from e


async def _post_degraded_to_slack(
    slack_service: SlackService | None,
    message: str,
    parsed: ParsedRequest | None,
    note: str,
) -> None:
    """Post a degraded-mode message to Slack.

    The header is the raw listener message; the context line carries the
    italicized reason and (when available) any parsed fields the DJ can use.
    """
    if not slack_service:
        logger.info("Slack integration disabled, skipping degraded post")
        return

    context_segments = [f"_{note}_"]
    if parsed is not None:
        context_segments.extend(_parsed_context_parts(parsed))
    context = " | ".join(context_segments)
    blocks = build_simple_slack_blocks(message, context)

    try:
        await slack_service.post_blocks(blocks)
    except Exception as e:
        logger.error(f"Failed to post degraded message to Slack: {e}")
        raise HTTPException(status_code=502, detail=f"Failed to post to Slack: {e}") from e


@router.post(
    "/request",
    response_model=UnifiedResponse,
    summary="Process song request",
    description="""
    Complete workflow: parse song request, search library, find artwork, and post to Slack.

    This endpoint:
    1. Parses the message using AI to extract song/album/artist
    2. Searches the library catalog for matches
    3. Fetches album artwork from external providers
    4. Posts enriched results to Slack
    5. Returns combined results

    Example request:
    ```json
    {
        "message": "Play Bohemian Rhapsody by Queen"
    }
    ```

    The response includes:
    - Parsed metadata (song, artist, album, request type)
    - Library search results with catalog info
    - Album artwork URLs where available
    - degraded_mode field when an upstream dependency was unavailable
      ("parsing_unavailable" if Groq failed; "search_unavailable" if LML failed)
    """,
    responses={
        200: {"description": "Request processed (possibly via a degraded fallback)"},
        400: {"description": "Invalid request (empty message)"},
        502: {"description": "Slack unavailable"},
    },
)
async def handle_request(
    request: RequestBody,
    groq_client: AsyncGroq = Depends(get_groq_client),
    slack_service: SlackService | None = Depends(get_slack_service),
    posthog_client: Posthog | None = Depends(get_posthog_client),
    lookup_client: LookupServiceClient | None = Depends(get_lookup_client),
):
    """Parse a request, search the library, and post to Slack.

    On Groq or LML failure, fall back to posting the raw / parsed message with
    a degraded-mode note rather than returning an error.
    """
    if not request.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    # Initialize telemetry
    init_cache_stats()
    telemetry = RequestTelemetry()

    # ------------------------------------------------------------------
    # Step 1: Parse the message. Any failure here -> degraded "parsing" path.
    # ------------------------------------------------------------------
    try:
        with telemetry.track_step("parse"):
            telemetry.record_api_call("groq")
            parsed = await parse_request(request.message, groq_client)
            logger.info(
                f"Parsed request: is_request={parsed.is_request}, type={parsed.message_type}"
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(
            "Parsing unavailable (%s: %s); posting raw message to Slack",
            type(e).__name__,
            e,
        )
        if not request.skip_slack:
            telemetry.record_api_call("slack")
            await _post_degraded_to_slack(
                slack_service,
                request.message,
                parsed=None,
                note="Parsing unavailable",
            )
        if posthog_client:
            telemetry.send_to_posthog(
                posthog_client,
                {
                    "results_count": 0,
                    "search_type": "none",
                    "had_artist": False,
                    "had_album": False,
                    "had_song": False,
                    "is_request": None,
                    "message_type": None,
                    "degraded_mode": DEGRADED_PARSING,
                    "degraded_reason": type(e).__name__,
                },
            )
        return UnifiedResponse(
            parsed=None,
            cache_stats=get_cache_stats(),
            degraded_mode=DEGRADED_PARSING,
        )

    # ------------------------------------------------------------------
    # Non-requests skip the search pipeline entirely.
    # ------------------------------------------------------------------
    if not parsed.is_request:
        if not request.skip_slack:
            await post_results_to_slack(slack_service, request.message, parsed, [], context=None)
        return UnifiedResponse(
            parsed=parsed,
            cache_stats=get_cache_stats(),
        )

    # ------------------------------------------------------------------
    # Step 2: Search via LML. Failures or missing config -> degraded "search" path.
    # ------------------------------------------------------------------
    library_results: list[LibraryItem] = []
    items_with_artwork: list[tuple[LibraryItem, ReleaseMetadata | None]] = []
    song_not_found = False
    found_on_compilation = False
    context: str | None = None
    cache_stats_override: dict | None = None
    search_type = "none"
    lookup_response = None
    lookup_failure: Exception | None = None

    if lookup_client is None:
        # Treat missing LOOKUP_SERVICE_URL as a permanent LML outage rather than
        # a hard 503. Operators see the degraded_mode signal in telemetry.
        logger.warning("LOOKUP_SERVICE_URL not configured; entering search-degraded mode")
        lookup_failure = RuntimeError("LOOKUP_SERVICE_URL not configured")
    else:
        with telemetry.track_step("lookup_service"):
            lookup_request = LookupRequest(
                artist=parsed.artist,
                song=parsed.song,
                album=parsed.album,
                raw_message=request.message,
            )
            try:
                lookup_response = await lookup_client.lookup(
                    lookup_request, skip_cache=request.skip_cache
                )
            except _LML_TRANSIENT_ERRORS as e:
                logger.warning(
                    "Search unavailable (%s: %s); posting parsed message to Slack",
                    type(e).__name__,
                    e,
                )
                lookup_failure = e

    if lookup_response is not None:
        # Apply corrected artist for Slack display
        if lookup_response.corrected_artist:
            parsed.artist = lookup_response.corrected_artist

        # Extract results for Slack posting. Generated LookupResponse fields
        # are nullable in the schema; coerce to non-null defaults for callers.
        results = lookup_response.results or []
        library_results = [item.library_item for item in results]
        items_with_artwork = [(item.library_item, item.artwork) for item in results]
        search_type = str(lookup_response.search_type or "none")
        song_not_found = bool(lookup_response.song_not_found)
        found_on_compilation = bool(lookup_response.found_on_compilation)
        context = lookup_response.context_message

        # Prefer the lookup service's cache stats over local counters,
        # which stay at 0 since all Discogs/cache work is delegated.
        if lookup_response.cache_stats:
            cache_stats_override = lookup_response.cache_stats

    # ------------------------------------------------------------------
    # Step 3: Post to Slack (search-success path or search-degraded path).
    # ------------------------------------------------------------------
    with telemetry.track_step("slack_post"):
        if not request.skip_slack:
            telemetry.record_api_call("slack")
            if lookup_failure is not None:
                await _post_degraded_to_slack(
                    slack_service,
                    request.message,
                    parsed=parsed,
                    note="Search unavailable",
                )
            else:
                await post_results_to_slack(
                    slack_service, request.message, parsed, items_with_artwork, context
                )

    # Extract main artwork from first result
    artwork = (
        next((art for _, art in items_with_artwork if art), None) if items_with_artwork else None
    )

    # Send telemetry
    degraded_mode = DEGRADED_SEARCH if lookup_failure is not None else None
    if posthog_client:
        properties: dict = {
            "results_count": len(library_results),
            "search_type": search_type,
            "had_artist": bool(parsed.artist),
            "had_album": bool(parsed.album),
            "had_song": bool(parsed.song),
            "is_request": parsed.is_request,
            "message_type": parsed.message_type.value if parsed.message_type else None,
        }
        if degraded_mode:
            properties["degraded_mode"] = degraded_mode
            properties["degraded_reason"] = type(lookup_failure).__name__
        telemetry.send_to_posthog(posthog_client, properties)

    return UnifiedResponse(
        parsed=parsed,
        artwork=artwork,
        library_results=library_results,
        search_type=search_type,
        song_not_found=song_not_found,
        found_on_compilation=found_on_compilation,
        context_message=context,
        cache_stats=cache_stats_override or get_cache_stats(),
        degraded_mode=degraded_mode,
    )
