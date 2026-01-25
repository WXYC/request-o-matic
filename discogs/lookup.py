"""Helper functions for Discogs lookups.

These functions create their own service instances for use internally
by the request handler, bypassing FastAPI dependency injection.
"""

import logging
from typing import Optional

from discogs.models import DiscogsSearchRequest
from discogs.service import DiscogsService

logger = logging.getLogger(__name__)


def _get_service() -> Optional[DiscogsService]:
    """Get a DiscogsService instance if token is configured."""
    from config.settings import get_settings

    settings = get_settings()
    if not settings.discogs_token:
        return None
    return DiscogsService(settings.discogs_token)


async def lookup_releases_by_track(
    track: str,
    artist: Optional[str] = None,
    limit: int = 20,
) -> list[tuple[str, str]]:
    """Look up all releases containing a track using Discogs.

    For Various Artists / compilation releases, validates the tracklist
    to ensure the track by the artist actually exists on the release.

    Args:
        track: Track title
        artist: Optional artist name
        limit: Maximum number of results

    Returns:
        List of (artist, album) tuples for releases containing the track.
        Useful for finding compilations and alternate releases.
    """
    service = _get_service()
    if not service:
        return []

    response = await service.search_releases_by_track(track, artist, limit)

    # Validate that the track actually exists on each release
    releases = []
    for release_info in response.releases:
        if artist and release_info.release_id:
            is_valid = await service.validate_track_on_release(
                release_info.release_id, track, artist
            )
            if not is_valid:
                logger.info(
                    f"Skipping '{release_info.album}' - track/artist not validated on release"
                )
                continue

        releases.append((release_info.artist, release_info.album))

    return releases


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
    service = _get_service()
    if not service:
        return []

    request = DiscogsSearchRequest(artist=artist)
    response = await service.search(request, limit=limit)

    return [(r.artist or "", r.album or "") for r in response.results]
