import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from artwork.models import ArtworkRequest, ArtworkResponse
from artwork.router import get_finder
from library.models import LibraryItem
from library.router import get_db
from services.groq import get_groq_client
from services.parser import ParsedRequest, parse_request
from services.slack import build_slack_blocks, post_to_slack

logger = logging.getLogger(__name__)

router = APIRouter(tags=["request"])


class RequestBody(BaseModel):
    message: str


class UnifiedResponse(BaseModel):
    """Combined response from parsing, artwork lookup, and library search."""
    parsed: ParsedRequest
    artwork: Optional[ArtworkResponse] = None
    library_results: list[LibraryItem] = []


async def fetch_artwork_for_item(item: LibraryItem) -> Optional[str]:
    """Fetch artwork URL for a library item from Discogs."""
    try:
        finder = get_finder()
        result = await finder.find(ArtworkRequest(
            album=item.title,
            artist=item.artist,
        ))
        return result.artwork_url if result else None
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
        items_with_artwork: list[tuple[LibraryItem, Optional[str]]] = []

        # Step 2: If we have artist/album info, search library first
        has_search_info = parsed.artist or parsed.album
        if has_search_info:
            try:
                db = get_db()
                query_parts = []
                if parsed.artist:
                    query_parts.append(parsed.artist)
                if parsed.album:
                    query_parts.append(parsed.album)
                query = " ".join(query_parts)
                library_results = await db.search(query=query, limit=5)
            except Exception as e:
                logger.warning(f"Library search failed: {e}")

            # Step 3: Fetch artwork for each library item in parallel
            if library_results:
                artwork_urls = await asyncio.gather(
                    *[fetch_artwork_for_item(item) for item in library_results]
                )
                items_with_artwork = list(zip(library_results, artwork_urls))

                # Use first artwork as the main artwork response
                first_artwork_url = next((url for url in artwork_urls if url), None)
                if first_artwork_url:
                    first_item = library_results[0]
                    artwork = ArtworkResponse(
                        artwork_url=first_artwork_url,
                        album=first_item.title,
                        artist=first_item.artist,
                        source="discogs",
                        confidence=0.5,
                    )

        # Step 4: Build and post to Slack
        if items_with_artwork:
            blocks = build_slack_blocks(request.message, items_with_artwork)
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
