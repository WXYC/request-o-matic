"""Artwork router with dependency injection."""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from artwork.finder import ArtworkFinder
from artwork.models import ArtworkRequest, ArtworkResponse
from core.dependencies import get_artwork_finder

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/artwork", tags=["artwork"])


@router.post(
    "",
    response_model=ArtworkResponse,
    summary="Find album artwork",
    description="""
    Find album artwork for a given song, album, or artist from external providers.
    
    Searches multiple artwork providers (currently Discogs) to find the best match.
    
    Example request:
    ```json
    {
        "song": "Bohemian Rhapsody",
        "artist": "Queen",
        "album": "A Night at the Opera"
    }
    ```
    """,
    responses={
        200: {"description": "Artwork found and returned"},
        400: {"description": "Invalid request (no search parameters)"},
        503: {"description": "Artwork service not available"},
        500: {"description": "Internal server error"},
    },
)
async def find_artwork(
    request: ArtworkRequest,
    finder: Optional[ArtworkFinder] = Depends(get_artwork_finder),
):
    """Find album artwork for the given song/album/artist."""
    if not request.song and not request.album and not request.artist:
        raise HTTPException(
            status_code=400,
            detail="At least one of song, album, or artist must be provided",
        )

    if finder is None:
        raise HTTPException(
            status_code=503,
            detail="Artwork lookup is disabled or not configured",
        )

    try:
        result = await finder.find(request)
        return result
    except Exception as e:
        logger.error(f"Error finding artwork: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
