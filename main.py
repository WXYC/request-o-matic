import asyncio
import logging
import os
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from groq import Groq
from pydantic import BaseModel

from parser import ParsedRequest, parse_request, MessageType
from artwork_service import router as artwork_router, init_artwork_service, shutdown_artwork_service, get_finder
from artwork.models import ArtworkRequest, ArtworkResponse
from library_service import router as library_router, init_library_service, shutdown_library_service, get_db
from library.models import LibraryItem

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Request-O-Matic",
    description="Supplement song requests with structured metadata, album artwork, and library catalog info",
    version="1.0.0",
)

app.include_router(artwork_router)
app.include_router(library_router)

client: Groq | None = None
slack_webhook_url: str | None = None
http_client: httpx.AsyncClient | None = None


@app.on_event("startup")
async def startup():
    global client, slack_webhook_url, http_client
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        logger.error("GROQ_API_KEY environment variable not set")
        raise RuntimeError("GROQ_API_KEY environment variable not set")
    client = Groq(api_key=api_key)
    logger.info("Groq client initialized")

    # Initialize HTTP client
    http_client = httpx.AsyncClient(timeout=30.0)

    # Check for webhook URL override, otherwise fetch from Railway
    slack_webhook_url = os.getenv("SLACK_WEBHOOK_URL")
    if slack_webhook_url:
        logger.info("Using Slack webhook URL from environment")
    else:
        try:
            response = await http_client.get("https://wxyc-requests-endpoint-production.up.railway.app")
            response.raise_for_status()
            webhook_key = response.text.strip()
            slack_webhook_url = f"https://hooks.slack.com/services/{webhook_key}"
            logger.info("Slack webhook URL configured from Railway")
        except Exception as e:
            logger.error(f"Failed to fetch Slack webhook key: {e}")
            raise RuntimeError(f"Failed to fetch Slack webhook key: {e}")

    init_artwork_service()
    await init_library_service()


@app.on_event("shutdown")
async def shutdown():
    global http_client
    await shutdown_artwork_service()
    await shutdown_library_service()
    if http_client:
        await http_client.aclose()
        http_client = None
    logger.info("Services shut down")


class ParseRequest(BaseModel):
    message: str


class UnifiedResponse(BaseModel):
    """Combined response from parsing, artwork lookup, and library search."""
    parsed: ParsedRequest
    artwork: Optional[ArtworkResponse] = None
    library_results: list[LibraryItem] = []


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/parse", response_model=ParsedRequest)
async def parse(request: ParseRequest):
    if client is None:
        raise HTTPException(status_code=503, detail="Groq client not initialized")

    if not request.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    try:
        result = parse_request(request.message, client)
        logger.info(f"Parsed request: is_request={result.is_request}, type={result.message_type}")
        return result
    except ValueError as e:
        logger.error(f"Parsing error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


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


def build_slack_blocks(message: str, items_with_artwork: list[tuple[LibraryItem, Optional[str]]]) -> list[dict]:
    """Build Slack message blocks from library results with artwork."""
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{message}*"
            }
        }
    ]

    for item, artwork_url in items_with_artwork:
        block: dict = {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{item.artist or 'Unknown Artist'}*\n{item.title or 'Unknown Title'}\n_{item.call_number}_"
            }
        }

        if artwork_url:
            block["accessory"] = {
                "type": "image",
                "image_url": artwork_url,
                "alt_text": f"{item.title} album cover"
            }

        blocks.append(block)

    return blocks


async def post_to_slack(blocks: list[dict]) -> None:
    """Post message blocks to Slack webhook."""
    if not slack_webhook_url or not http_client:
        raise RuntimeError("Slack webhook not configured")

    response = await http_client.post(
        slack_webhook_url,
        json={"blocks": blocks}
    )
    response.raise_for_status()
    logger.info("Posted to Slack successfully")


@app.post("/request", response_model=UnifiedResponse)
async def handle_request(request: ParseRequest):
    """
    Unified endpoint: parse a song request, find artwork, search the library, and post to Slack.

    1. Parses the message to extract song/album/artist
    2. Searches the library catalog
    3. Fetches artwork for each library result in parallel
    4. Posts enriched results to Slack
    5. Returns combined results
    """
    if client is None:
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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
