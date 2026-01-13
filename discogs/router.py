"""FastAPI router for Discogs API endpoints."""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from core.dependencies import get_discogs_service
from discogs.models import (
    DiscogsSearchRequest,
    DiscogsSearchResponse,
    ReleaseMetadataResponse,
    TrackAlbumResponse,
    TrackReleasesResponse,
)
from discogs.service import DiscogsService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/discogs", tags=["discogs"])


def _require_service(service: Optional[DiscogsService]) -> DiscogsService:
    """Raise 503 if service is not available."""
    if service is None:
        raise HTTPException(
            status_code=503,
            detail="Discogs service is not configured. Set DISCOGS_TOKEN environment variable.",
        )
    return service


@router.get(
    "/track-album",
    response_model=TrackAlbumResponse,
    summary="Find album containing a track",
    description="""
    Search Discogs for a track and return the album that contains it.

    Useful for finding which album a song appears on when you only know
    the track name.

    Example:
    ```
    GET /api/v1/discogs/track-album?track=VI+Scose+Poise&artist=Autechre
    ```
    """,
    responses={
        200: {"description": "Album information returned"},
        422: {"description": "Missing required track parameter"},
        503: {"description": "Discogs service not configured"},
    },
)
async def get_track_album(
    track: str = Query(..., description="Track/song title to search for"),
    artist: Optional[str] = Query(None, description="Optional artist name for filtering"),
    service: Optional[DiscogsService] = Depends(get_discogs_service),
) -> TrackAlbumResponse:
    """Find the album containing a specific track."""
    svc = _require_service(service)
    return await svc.search_track(track, artist)


@router.get(
    "/track-releases",
    response_model=TrackReleasesResponse,
    summary="Find all releases containing a track",
    description="""
    Search Discogs for ALL releases containing a track.

    Returns a list of releases (albums, compilations, singles) where the
    track appears. Useful for finding alternative versions or compilations.

    Example:
    ```
    GET /api/v1/discogs/track-releases?track=VI+Scose+Poise&artist=Autechre&limit=10
    ```
    """,
    responses={
        200: {"description": "List of releases returned"},
        422: {"description": "Missing required track parameter"},
        503: {"description": "Discogs service not configured"},
    },
)
async def get_track_releases(
    track: str = Query(..., description="Track/song title to search for"),
    artist: Optional[str] = Query(None, description="Optional artist name for filtering"),
    limit: int = Query(20, ge=1, le=100, description="Maximum number of results"),
    service: Optional[DiscogsService] = Depends(get_discogs_service),
) -> TrackReleasesResponse:
    """Find all releases containing a specific track."""
    svc = _require_service(service)
    return await svc.search_releases_by_track(track, artist, limit)


@router.get(
    "/release/{release_id}",
    response_model=ReleaseMetadataResponse,
    summary="Get full release metadata",
    description="""
    Get complete metadata for a Discogs release by ID.

    Returns detailed information including:
    - Title, artist, year, label
    - Genres and styles
    - Full tracklist with durations
    - Artwork URL

    Example:
    ```
    GET /api/v1/discogs/release/28138
    ```
    """,
    responses={
        200: {"description": "Release metadata returned"},
        404: {"description": "Release not found"},
        503: {"description": "Discogs service not configured"},
    },
)
async def get_release(
    release_id: int,
    service: Optional[DiscogsService] = Depends(get_discogs_service),
) -> ReleaseMetadataResponse:
    """Get full metadata for a release by ID."""
    svc = _require_service(service)
    result = await svc.get_release(release_id)

    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"Release {release_id} not found",
        )

    return result


@router.post(
    "/search",
    response_model=DiscogsSearchResponse,
    summary="Search Discogs releases",
    description="""
    General search for Discogs releases.

    Search by any combination of artist, album, or track name.
    Results include artwork URLs and confidence scores.

    Example:
    ```json
    POST /api/v1/discogs/search?limit=10
    {
        "artist": "Autechre",
        "album": "Confield"
    }
    ```
    """,
    responses={
        200: {"description": "Search results returned"},
        400: {"description": "No search parameters provided"},
        503: {"description": "Discogs service not configured"},
    },
)
async def search_releases(
    request: DiscogsSearchRequest,
    limit: int = Query(5, ge=1, le=50, description="Maximum number of results"),
    service: Optional[DiscogsService] = Depends(get_discogs_service),
) -> DiscogsSearchResponse:
    """Search Discogs for releases matching the criteria."""
    svc = _require_service(service)

    if not request.artist and not request.album and not request.track:
        raise HTTPException(
            status_code=400,
            detail="At least one of artist, album, or track must be provided",
        )

    return await svc.search(request, limit=limit)
