import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from artwork.models import ArtworkRequest, ArtworkResponse
from artwork.router import get_finder, lookup_album_by_track
from library.models import LibraryItem
from library.router import get_db
from services.groq import get_groq_client
from services.parser import MessageType, ParsedRequest, parse_request

# Friendly labels for message types in Slack
MESSAGE_TYPE_LABELS = {
    MessageType.REQUEST: "Song Request",
    MessageType.DJ_MESSAGE: "Message to DJ",
    MessageType.FEEDBACK: "Feedback",
    MessageType.OTHER: "Other",
}
from services.slack import build_slack_blocks, build_simple_slack_blocks, post_to_slack

logger = logging.getLogger(__name__)

router = APIRouter(tags=["request"])


class RequestBody(BaseModel):
    message: str


class UnifiedResponse(BaseModel):
    """Combined response from parsing, artwork lookup, and library search."""
    parsed: ParsedRequest
    artwork: Optional[ArtworkResponse] = None
    library_results: list[LibraryItem] = []


async def fetch_artwork_for_item(item: LibraryItem) -> Optional[ArtworkResponse]:
    """Fetch artwork for a library item from Discogs."""
    try:
        finder = get_finder()
        result = await finder.find(ArtworkRequest(
            album=item.title,
            artist=item.artist,
        ))
        return result
    except Exception as e:
        logger.warning(f"Artwork lookup failed for {item.title}: {e}")
        return None


@router.post("/request", response_model=UnifiedResponse)
async def handle_request(request: RequestBody):
    """
    Unified endpoint: parse a song request, find artwork, search the library, and post to Slack.

    1. Parses the message to extract song/album/artist
    2. Searches the library catalog
    3. Fetches artwork for each library result in parallel
    4. Posts enriched results to Slack
    5. Returns combined results
    """
    try:
        client = get_groq_client()
    except RuntimeError:
        raise HTTPException(status_code=503, detail="Groq client not initialized")

    if not request.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    try:
        # Step 1: Parse the message
        parsed = parse_request(request.message, client)
        logger.info(f"Parsed request: is_request={parsed.is_request}, type={parsed.message_type}")

        artwork: Optional[ArtworkResponse] = None
        library_results: list[LibraryItem] = []
        items_with_artwork: list[tuple[LibraryItem, Optional[ArtworkResponse]]] = []
        song_not_found = False  # Track if we couldn't find the requested song

        # Step 2: If we have a song but no album, look up the album from Discogs
        album_for_search = parsed.album
        if parsed.song and not parsed.album:
            try:
                album_from_track = await lookup_album_by_track(parsed.song, parsed.artist)
                if album_from_track:
                    album_for_search = album_from_track
                    logger.info(f"Found album '{album_from_track}' for song '{parsed.song}'")
                else:
                    # Song requested but album not found - we'll fall back to artist search
                    song_not_found = True
                    logger.info(f"Could not find album for song '{parsed.song}'")
            except Exception as e:
                logger.warning(f"Track lookup failed: {e}")
                song_not_found = True

        # Step 3: If we have artist/album info, search library
        has_search_info = parsed.artist or album_for_search
        if has_search_info:
            try:
                db = get_db()
                query_parts = []
                if parsed.artist:
                    query_parts.append(parsed.artist)
                if album_for_search:
                    query_parts.append(album_for_search)
                query = " ".join(query_parts)
                library_results = await db.search(query=query, limit=5)
                
                # If no results and we had both artist and album, try just artist
                if not library_results and parsed.artist and album_for_search:
                    logger.info(f"No results for '{query}', trying artist only: '{parsed.artist}'")
                    library_results = await db.search(query=parsed.artist, limit=5)
                    if library_results:
                        # Mark that we're showing artist albums, not the requested album
                        song_not_found = True
            except Exception as e:
                logger.warning(f"Library search failed: {e}")

            # Step 4: Fetch artwork for each library item in parallel
            if library_results:
                artwork_results = await asyncio.gather(
                    *[fetch_artwork_for_item(item) for item in library_results]
                )
                items_with_artwork = list(zip(library_results, artwork_results))

                # Use first artwork as the main artwork response
                artwork = next((result for result in artwork_results if result), None)

        # Step 5: Build and post to Slack
        if items_with_artwork:
            # Build context message if song/album wasn't found in library
            context = None
            if song_not_found:
                if parsed.song and parsed.album:
                    context = f"\"{parsed.album}\" not found in the library, but here are other albums by {parsed.artist}:"
                elif parsed.song:
                    context = f"\"{parsed.song}\" is not on any album in the library, but here are some albums by {parsed.artist}:"
            blocks = build_slack_blocks(request.message, items_with_artwork, context)
        elif not parsed.is_request:
            # Feedback or other non-request message
            label = MESSAGE_TYPE_LABELS.get(parsed.message_type, "Other")
            blocks = build_simple_slack_blocks(request.message, f"_{label}_")
        else:
            # Request but no results found
            context_parts = []
            if parsed.artist:
                context_parts.append(f"Artist: {parsed.artist}")
            if parsed.album:
                context_parts.append(f"Album: {parsed.album}")
            if parsed.song:
                context_parts.append(f"Song: {parsed.song}")
            context = " | ".join(context_parts) if context_parts else None
            blocks = build_simple_slack_blocks(request.message, f"_No results found_ {context or ''}")

        try:
            await post_to_slack(blocks)
        except Exception as e:
            logger.error(f"Failed to post to Slack: {e}")
            raise HTTPException(status_code=502, detail=f"Failed to post to Slack: {e}")

        return UnifiedResponse(
            parsed=parsed,
            artwork=artwork,
            library_results=library_results,
        )

    except HTTPException:
        raise
    except ValueError as e:
        logger.error(f"Parsing error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
