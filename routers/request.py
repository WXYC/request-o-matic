"""Request handling router that delegates search to library-metadata-lookup service.

Flow:
1. Parse the message using Groq AI to extract artist/song/album
2. Early return for non-requests (feedback, DJ messages, etc.)
3. Delegate search to library-metadata-lookup service via HTTP
4. Post enriched results to Slack
"""

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException
from groq import AsyncGroq, RateLimitError
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

router = APIRouter(tags=["request"])


class RequestBody(BaseModel):
    """Request body for song request parsing."""

    message: str
    skip_slack: bool = False
    skip_cache: bool = False


class UnifiedResponse(BaseModel):
    """Combined response from parsing, artwork lookup, and library search."""

    parsed: ParsedRequest
    artwork: ReleaseMetadata | None = None
    library_results: list[LibraryItem] = []
    # Search metadata
    search_type: str = "none"
    song_not_found: bool = False
    found_on_compilation: bool = False
    context_message: str | None = None
    cache_stats: dict | None = None


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
        context_parts = []
        if parsed.artist:
            context_parts.append(f"Artist: {parsed.artist}")
        if parsed.album:
            context_parts.append(f"Album: {parsed.album}")
        if parsed.song:
            context_parts.append(f"Song: {parsed.song}")
        ctx = " | ".join(context_parts) if context_parts else None
        blocks = build_simple_slack_blocks(message, f"_No results found_ {ctx or ''}")

    try:
        await slack_service.post_blocks(blocks)
    except Exception as e:
        logger.error(f"Failed to post to Slack: {e}")
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
    """,
    responses={
        200: {"description": "Request processed successfully"},
        400: {"description": "Invalid request (empty message)"},
        500: {"description": "Processing error"},
        502: {"description": "Lookup service or Slack unavailable"},
        503: {"description": "Search service not configured"},
    },
)
async def handle_request(
    request: RequestBody,
    groq_client: AsyncGroq = Depends(get_groq_client),
    slack_service: SlackService | None = Depends(get_slack_service),
    posthog_client: Posthog | None = Depends(get_posthog_client),
    lookup_client: LookupServiceClient | None = Depends(get_lookup_client),
):
    """
    Unified endpoint: parse a song request, find artwork, search the library, and post to Slack.

    1. Parses the message to extract song/album/artist
    2. Searches the library catalog
    3. Fetches artwork for each library result in parallel
    4. Posts enriched results to Slack
    5. Returns combined results
    """
    if not request.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    # Initialize telemetry
    init_cache_stats()
    telemetry = RequestTelemetry()
    search_type = "none"

    try:
        # Step 1: Parse the message
        with telemetry.track_step("parse"):
            telemetry.record_api_call("groq")
            parsed = await parse_request(request.message, groq_client)
            logger.info(
                f"Parsed request: is_request={parsed.is_request}, type={parsed.message_type}"
            )

        # Early return for non-requests (feedback, DJ messages, etc.)
        # No point running the search pipeline for messages that aren't song requests.
        if not parsed.is_request:
            if not request.skip_slack:
                await post_results_to_slack(
                    slack_service, request.message, parsed, [], context=None
                )
            return UnifiedResponse(
                parsed=parsed,
                cache_stats=get_cache_stats(),
            )

        if not lookup_client:
            raise HTTPException(status_code=503, detail="Search service not configured")

        library_results: list[LibraryItem] = []
        items_with_artwork: list[tuple[LibraryItem, ReleaseMetadata | None]] = []
        song_not_found = False
        found_on_compilation = False
        context: str | None = None
        cache_stats_override: dict | None = None

        # Delegate search to library-metadata-lookup service
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
            except (httpx.HTTPStatusError, httpx.ConnectError, httpx.TimeoutException) as e:
                logger.error(f"Lookup service error: {e}")
                raise HTTPException(status_code=502, detail="Lookup service unavailable") from e

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

        # Step 5: Post to Slack (unless skip_slack is set)
        with telemetry.track_step("slack_post"):
            if not request.skip_slack:
                telemetry.record_api_call("slack")
                await post_results_to_slack(
                    slack_service, request.message, parsed, items_with_artwork, context
                )

        # Extract main artwork from first result
        artwork = (
            next((art for _, art in items_with_artwork if art), None)
            if items_with_artwork
            else None
        )

        # Send telemetry
        if posthog_client:
            telemetry.send_to_posthog(
                posthog_client,
                {
                    "results_count": len(library_results),
                    "search_type": search_type,
                    "had_artist": bool(parsed.artist),
                    "had_album": bool(parsed.album),
                    "had_song": bool(parsed.song),
                    "is_request": parsed.is_request,
                    "message_type": parsed.message_type.value if parsed.message_type else None,
                },
            )

        return UnifiedResponse(
            parsed=parsed,
            artwork=artwork,
            library_results=library_results,
            search_type=search_type,
            song_not_found=song_not_found,
            found_on_compilation=found_on_compilation,
            context_message=context,
            cache_stats=cache_stats_override or get_cache_stats(),
        )

    except HTTPException:
        raise
    except RateLimitError as e:
        logger.warning(f"Groq rate limit exceeded: {e}")
        raise HTTPException(status_code=429, detail="Rate limit exceeded, please retry") from e
    except ValueError as e:
        logger.error(f"Parsing error: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e
