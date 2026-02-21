"""Request handling router with search orchestration delegated to core.orchestration.

Search Strategy Decision Tree
=============================

The search flow follows these steps in order:

1. PARSE: Extract artist/song/album from message using Groq AI

2. ARTIST CORRECTION: Fuzzy match artist against library to fix typos
   (e.g., "Living Color" -> "Living Colour")

3. ALBUM LOOKUP: If song provided without album, query Discogs for album name
   (e.g., "Two Headed Boy" -> "In the Aeroplane Over the Sea")

4. PRIMARY SEARCH: Search library with fallback chain:
   - artist + album (from Discogs lookup or parsed)
   - artist + song (song title might match album title)
   - artist only (fallback when song/album not found)

5. ALTERNATIVE INTERPRETATION: For ambiguous "X - Y" formats, try both:
   - X as artist, Y as title
   - Y as artist, X as title

6. COMPILATION SEARCH: If no results, cross-reference Discogs track listings
   with library to find song on compilations/soundtracks

Each step is tracked via telemetry for observability.
"""

import logging
from functools import partial

import httpx
from fastapi import APIRouter, Depends, HTTPException
from groq import Groq
from posthog import Posthog
from pydantic import BaseModel

from core.dependencies import (
    SlackService,
    get_discogs_service,
    get_groq_client,
    get_library_db,
    get_lookup_client,
    get_posthog_client,
    get_slack_service,
)
from core.matching import detect_ambiguous_format
from core.orchestration import (
    build_context_message,
    fetch_artwork_for_items,
    filter_results_by_track_validation,
    limit_results,
    resolve_albums_for_track,
    search_compilations_for_track,
    search_library_with_fallback,
    search_song_as_artist,
    search_with_alternative_interpretation,
)
from core.search import (
    SearchState,
    build_strategies,
    execute_search_pipeline,
    get_search_type_from_state,
)
from core.telemetry import RequestTelemetry, get_cache_stats, init_cache_stats
from discogs.memory_cache import set_skip_cache
from discogs.models import DiscogsSearchResult
from discogs.service import DiscogsService
from library.db import LibraryDB
from library.models import LibraryItem
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
    artwork: DiscogsSearchResult | None = None
    library_results: list[LibraryItem] = []
    # Search metadata
    search_type: str = "none"
    song_not_found: bool = False
    found_on_compilation: bool = False
    context_message: str | None = None
    cache_stats: dict | None = None


# =============================================================================
# Strategy Adapters
# =============================================================================
# These thin adapters wrap the orchestration functions to conform to the
# uniform strategy signature: (db, parsed, state) -> None, mutating state.


async def _search_library_strategy(
    db: LibraryDB, parsed: ParsedRequest, state: SearchState
) -> None:
    """Adapter: search library with fallback, updating state."""
    results, fallback_used = await search_library_with_fallback(db, parsed, state.albums_for_search)
    if results:
        state.results = results
    if fallback_used:
        state.song_not_found = True


async def _swapped_interpretation_strategy(
    db: LibraryDB, parsed: ParsedRequest, state: SearchState
) -> None:
    """Adapter: try both interpretations of 'X - Y' format, updating state."""
    parts = detect_ambiguous_format(parsed.raw_message)
    if parts:
        part1, part2 = parts
        results, _ = await search_with_alternative_interpretation(db, part1, part2)
    else:
        results = []
    if results:
        state.results = results
        state.song_not_found = False


async def _compilation_search_strategy(
    db: LibraryDB,
    parsed: ParsedRequest,
    state: SearchState,
    discogs_service: DiscogsService | None = None,
) -> None:
    """Adapter: search compilations for track, updating state."""
    results, discogs_titles = await search_compilations_for_track(
        db, parsed, discogs_service=discogs_service
    )
    if results:
        state.results = results
        state.found_on_compilation = True
        state.song_not_found = False
        state.discogs_titles = discogs_titles


async def _song_as_artist_strategy(
    db: LibraryDB,
    parsed: ParsedRequest,
    state: SearchState,
    discogs_service: DiscogsService | None = None,
) -> None:
    """Adapter: try parsed song as artist, updating state."""
    if not parsed.song:
        return
    results, _ = await search_song_as_artist(db, parsed.song, discogs_service=discogs_service)
    if results:
        state.results = results
        state.song_not_found = False


async def post_results_to_slack(
    slack_service: SlackService | None,
    message: str,
    parsed: ParsedRequest,
    items_with_artwork: list[tuple[LibraryItem, DiscogsSearchResult | None]],
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
        502: {"description": "Failed to post to Slack"},
    },
)
async def handle_request(
    request: RequestBody,
    groq_client: Groq = Depends(get_groq_client),
    db: LibraryDB = Depends(get_library_db),
    discogs_service: DiscogsService | None = Depends(get_discogs_service),
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
    if request.skip_cache:
        set_skip_cache(True)
    telemetry = RequestTelemetry()
    search_type = "none"

    try:
        # Step 1: Parse the message
        with telemetry.track_step("parse"):
            telemetry.record_api_call("groq")
            parsed = parse_request(request.message, groq_client)
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

        library_results: list[LibraryItem] = []
        items_with_artwork: list[tuple[LibraryItem, DiscogsSearchResult | None]] = []
        song_not_found = False
        found_on_compilation = False
        context: str | None = None

        if lookup_client:
            # Delegated mode: call library-metadata-lookup service
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

            # Extract results for Slack posting
            library_results = [item.library_item for item in lookup_response.results]
            items_with_artwork = [
                (item.library_item, item.artwork) for item in lookup_response.results
            ]
            search_type = lookup_response.search_type
            song_not_found = lookup_response.song_not_found
            found_on_compilation = lookup_response.found_on_compilation
            context = lookup_response.context_message

        else:
            # Inline mode: existing pipeline
            discogs_titles: dict[int, str] = {}

            # Step 1b: Correct artist spelling (e.g., "Living Color" -> "Living Colour")
            if parsed.artist:
                corrected_artist = await db.find_similar_artist(parsed.artist)
                if corrected_artist:
                    parsed.artist = corrected_artist

            # Step 2: If we have a song but no album, look up albums from Discogs
            with telemetry.track_step("album_lookup"):
                if parsed.song and not parsed.album:
                    telemetry.record_api_call("discogs")
                albums_for_search, song_not_found = await resolve_albums_for_track(
                    parsed, discogs_service
                )

            # Step 3: Execute search strategy pipeline
            # The pipeline tries strategies in order until results are found:
            # 1. ARTIST_PLUS_ALBUM - search by artist + album/song
            # 2. SWAPPED_INTERPRETATION - try "X - Y" as both orderings
            # 3. TRACK_ON_COMPILATION - find song on compilations via Discogs
            # 4. SONG_AS_ARTIST - try parsed song as artist (parser misidentification)
            with telemetry.track_step("library_search"):
                strategies = build_strategies(
                    search_library_func=_search_library_strategy,
                    search_alternative_func=_swapped_interpretation_strategy,
                    search_compilations_func=partial(
                        _compilation_search_strategy, discogs_service=discogs_service
                    ),
                    search_song_as_artist_func=partial(
                        _song_as_artist_strategy, discogs_service=discogs_service
                    ),
                )

                search_state = await execute_search_pipeline(
                    parsed=parsed,
                    db=db,
                    raw_message=request.message,
                    strategies=strategies,
                    albums_for_search=albums_for_search,
                )

                # Extract results from state
                library_results = limit_results(search_state.results)
                song_not_found = search_state.song_not_found
                found_on_compilation = search_state.found_on_compilation
                discogs_titles = search_state.discogs_titles
                search_type = get_search_type_from_state(search_state)

                # Record Discogs API call if compilation search was used
                if found_on_compilation:
                    telemetry.record_api_call("discogs")

            # Step 3b: Validate fallback results against Discogs track data
            # When the pipeline fell back to returning all artist albums, try to
            # filter to only albums that actually contain the requested track.
            if song_not_found and library_results and parsed.song and parsed.artist:
                with telemetry.track_step("track_validation"):
                    validated = await filter_results_by_track_validation(
                        library_results, parsed.song, parsed.artist, discogs_service
                    )
                    if validated:
                        library_results = validated
                        song_not_found = False

            # Step 4: Fetch artwork for library items
            with telemetry.track_step("artwork_fetch"):
                if library_results:
                    # Count Discogs API calls for artwork (one per item)
                    for _ in library_results:
                        telemetry.record_api_call("discogs")
                    items_with_artwork = await fetch_artwork_for_items(
                        library_results, discogs_service, discogs_titles
                    )

            context = build_context_message(
                parsed, found_on_compilation, song_not_found, has_results=bool(library_results)
            )

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
            cache_stats=get_cache_stats(),
        )

    except HTTPException:
        raise
    except ValueError as e:
        logger.error(f"Parsing error: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e
