"""Artwork router with dependency injection."""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from artwork.finder import ArtworkFinder
from artwork.models import ArtworkRequest, ArtworkResponse
from artwork.providers.discogs import DiscogsProvider
from core.dependencies import get_artwork_finder

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/artwork", tags=["artwork"])


async def lookup_album_by_track(
    track: str,
    artist: Optional[str] = None,
) -> Optional[str]:
    """Look up an album name by track title using Discogs.

    Note: This function creates a temporary provider instance to avoid
    dependency injection complexity when called from helper functions.

    Args:
        track: Track title
        artist: Optional artist name

    Returns:
        Album name if found, None otherwise
    """
    # Import here to avoid circular imports
    from config.settings import get_settings

    settings = get_settings()
    if not settings.discogs_token:
        return None

    provider = DiscogsProvider(settings.discogs_token)
    return await provider.search_track(track, artist)


async def lookup_releases_by_track(
    track: str,
    artist: Optional[str] = None,
    limit: int = 20,
) -> list[tuple[str, str]]:
    """Look up all releases containing a track using Discogs.

    Note: This function creates a temporary finder instance to avoid
    dependency injection complexity in helper functions.

    Args:
        track: Track title
        artist: Optional artist name
        limit: Maximum number of results

    Returns:
        List of (artist, album) tuples for releases containing the track.
        Useful for finding compilations and alternate releases.
    """
    # Import here to avoid circular imports
    from config.settings import get_settings

    settings = get_settings()
    if not settings.discogs_token:
        return []

    provider = DiscogsProvider(settings.discogs_token)
    return await provider.search_releases_by_track(track, artist, limit)


async def lookup_releases_by_artist(
    artist: str,
    limit: int = 10,
) -> list[tuple[str, str]]:
    """Look up releases by an artist using Discogs.

    Searches for releases where the artist appears, including compilations.

    Args:
        artist: Artist name to search for
        limit: Maximum number of results

    Returns:
        List of (artist, album) tuples for releases by or featuring the artist.
    """
    from config.settings import get_settings
    from discogs.models import DiscogsSearchRequest
    from discogs.service import DiscogsService

    settings = get_settings()
    if not settings.discogs_token:
        return []

    service = DiscogsService(settings.discogs_token)
    request = DiscogsSearchRequest(artist=artist)
    response = await service.search(request, limit=limit)

    return [(r.artist, r.album) for r in response.results]


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
