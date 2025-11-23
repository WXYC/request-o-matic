import logging
import os

from fastapi import APIRouter, HTTPException

from artwork.models import ArtworkRequest, ArtworkResponse
from artwork.finder import ArtworkFinder
from artwork.providers.discogs import DiscogsProvider

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/artwork", tags=["artwork"])

_finder: ArtworkFinder | None = None


def get_finder() -> ArtworkFinder:
    """Get or create the ArtworkFinder instance."""
    global _finder
    if _finder is None:
        raise RuntimeError("ArtworkFinder not initialized")
    return _finder


def init_artwork_service():
    """Initialize the artwork service with configured providers."""
    global _finder

    providers = []

    discogs_token = os.getenv("DISCOGS_TOKEN")
    if discogs_token:
        providers.append(DiscogsProvider(discogs_token))
        logger.info("Discogs provider initialized")
    else:
        logger.warning("DISCOGS_TOKEN not set - Discogs provider disabled")

    if not providers:
        logger.warning("No artwork providers configured")

    _finder = ArtworkFinder(providers)
    logger.info(f"Artwork service initialized with {len(providers)} provider(s)")


async def shutdown_artwork_service():
    """Clean up artwork service resources."""
    global _finder
    if _finder:
        for provider in _finder.providers:
            if hasattr(provider, "close"):
                await provider.close()
    _finder = None
    logger.info("Artwork service shut down")


@router.post("", response_model=ArtworkResponse)
async def find_artwork(request: ArtworkRequest):
    """Find album artwork for the given song/album/artist."""
    if not request.song and not request.album and not request.artist:
        raise HTTPException(
            status_code=400,
            detail="At least one of song, album, or artist must be provided",
        )

    try:
        finder = get_finder()
        result = await finder.find(request)
        return result
    except RuntimeError as e:
        logger.error(f"Service not initialized: {e}")
        raise HTTPException(status_code=503, detail="Artwork service not initialized")
    except Exception as e:
        logger.error(f"Error finding artwork: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
