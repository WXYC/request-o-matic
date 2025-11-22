import asyncio
import logging
import os
from typing import Optional

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


@app.on_event("startup")
async def startup():
    global client
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        logger.error("GROQ_API_KEY environment variable not set")
        raise RuntimeError("GROQ_API_KEY environment variable not set")
    client = Groq(api_key=api_key)
    logger.info("Groq client initialized")

    init_artwork_service()
    await init_library_service()


@app.on_event("shutdown")
async def shutdown():
    await shutdown_artwork_service()
    await shutdown_library_service()
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


@app.post("/request", response_model=UnifiedResponse)
async def handle_request(request: ParseRequest):
    """
    Unified endpoint: parse a song request, find artwork, and search the library.

    1. Parses the message to extract song/album/artist
    2. If it's a request, looks up artwork on Discogs and searches the library
    3. Returns combined results
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

        # Step 2: If we have artist/album info, query artwork and library in parallel
        has_search_info = parsed.artist or parsed.album
        if has_search_info:
            # Artwork lookup task
            async def fetch_artwork() -> Optional[ArtworkResponse]:
                try:
                    finder = get_finder()
                    return await finder.find(ArtworkRequest(
                        song=parsed.song,
                        album=parsed.album,
                        artist=parsed.artist,
                    ))
                except Exception as e:
                    logger.warning(f"Artwork lookup failed: {e}")
                    return None

            # Library search task
            async def search_library() -> list[LibraryItem]:
                try:
                    db = get_db()
                    # Build a search query from available info
                    query_parts = []
                    if parsed.artist:
                        query_parts.append(parsed.artist)
                    if parsed.album:
                        query_parts.append(parsed.album)
                    query = " ".join(query_parts)
                    return await db.search(query=query, limit=5)
                except Exception as e:
                    logger.warning(f"Library search failed: {e}")
                    return []

            # Run both in parallel
            artwork_result, library_result = await asyncio.gather(
                fetch_artwork(),
                search_library(),
            )
            artwork = artwork_result
            library_results = library_result

        return UnifiedResponse(
            parsed=parsed,
            artwork=artwork,
            library_results=library_results,
        )

    except ValueError as e:
        logger.error(f"Parsing error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
