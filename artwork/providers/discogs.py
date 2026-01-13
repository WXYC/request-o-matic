"""Discogs artwork provider that delegates to DiscogsService."""
import logging
from typing import Optional

from artwork.models import ArtworkRequest, SearchResult
from discogs.models import DiscogsSearchRequest
from discogs.service import DiscogsService

logger = logging.getLogger(__name__)


class DiscogsProvider:
    """Artwork provider using the Discogs API.

    Delegates to DiscogsService for API calls to avoid code duplication.
    Can be initialized with either a token (creates internal service) or
    an existing DiscogsService instance.
    """

    def __init__(self, token: Optional[str] = None, service: Optional[DiscogsService] = None):
        """Initialize the provider.

        Args:
            token: Discogs API token. Creates an internal DiscogsService.
            service: Existing DiscogsService instance to use.

        Either token or service must be provided.
        """
        if service is not None:
            self._service = service
            self._owns_service = False
        elif token is not None:
            self._service = DiscogsService(token)
            self._owns_service = True
        else:
            raise ValueError("Either token or service must be provided")

    @property
    def name(self) -> str:
        return "discogs"

    async def close(self):
        """Close the provider's resources."""
        if self._owns_service:
            await self._service.close()

    async def search_track(self, track: str, artist: Optional[str] = None) -> Optional[str]:
        """Search Discogs for a track and return the album name that contains it."""
        result = await self._service.search_track(track, artist)
        return result.album if result else None

    async def search_releases_by_track(
        self, track: str, artist: Optional[str] = None, limit: int = 20
    ) -> list[tuple[str, str]]:
        """Search Discogs for ALL releases containing a track.

        For Various Artists / compilation releases, validates the tracklist
        to ensure the track by the artist actually exists on the release.

        Returns:
            List of (artist, album) tuples for releases containing the track.
        """
        response = await self._service.search_releases_by_track(track, artist, limit)

        # If searching with artist, validate compilation releases
        releases = []
        for release_info in response.releases:
            # For Various Artists / compilations, validate the tracklist
            # Use service's compilation detection to avoid duplication
            if artist and self._service._is_compilation_artist(release_info.artist):
                is_valid = await self._service.validate_track_on_release(
                    release_info.release_id, track, artist
                )
                if not is_valid:
                    logger.info(
                        f"Skipping '{release_info.album}' - track/artist not validated on release"
                    )
                    continue

            releases.append((release_info.artist, release_info.album))

        return releases

    async def validate_track_on_release(
        self, release_id: int, track: str, artist: str
    ) -> bool:
        """Validate that a track by an artist exists on a release.

        Delegates to DiscogsService.
        """
        return await self._service.validate_track_on_release(release_id, track, artist)

    async def search(self, request: ArtworkRequest) -> list[SearchResult]:
        """Search Discogs for album artwork.

        Converts ArtworkRequest to DiscogsSearchRequest and delegates to service.
        """
        # Build search request
        discogs_request = DiscogsSearchRequest(
            artist=request.artist,
            album=request.album,
            track=request.song,
        )

        # Check if there's anything to search for
        if not discogs_request.artist and not discogs_request.album and not discogs_request.track:
            logger.warning("No searchable fields in request")
            return []

        # Delegate to service
        response = await self._service.search(discogs_request)

        # Convert results to SearchResult format
        results = []
        for item in response.results:
            # Skip results without artwork
            if not item.artwork_url or "spacer.gif" in item.artwork_url:
                continue

            # Recalculate confidence with local method (for ArtworkRequest fields)
            confidence = self._calculate_confidence(request, item.artist or "", item.album or "")

            results.append(
                SearchResult(
                    artwork_url=item.artwork_url,
                    release_url=item.release_url,
                    album=item.album,
                    artist=item.artist,
                    source=self.name,
                    confidence=confidence,
                )
            )

        results.sort(key=lambda r: r.confidence, reverse=True)
        return results

    def _build_search_params(self, request: ArtworkRequest) -> dict:
        """Build search params - kept for backwards compatibility with tests."""
        params: dict = {
            "type": "release",
            "per_page": 5,
        }

        if request.artist:
            params["artist"] = request.artist
        if request.album:
            params["release_title"] = request.album
        elif request.song:
            params["release_title"] = request.song

        if "artist" not in params and "release_title" not in params:
            return {}

        return params

    def _calculate_confidence(
        self, request: ArtworkRequest, result_artist: str, result_album: str
    ) -> float:
        """Calculate confidence score based on how well result matches request."""
        score = 0.0

        def normalize(s: str) -> str:
            return s.lower().strip() if s else ""

        req_artist = normalize(request.artist or "")
        req_album = normalize(request.album or "")
        res_artist = normalize(result_artist)
        res_album = normalize(result_album)

        # Artist match
        if req_artist and res_artist:
            if req_artist == res_artist:
                score += 0.4
            elif req_artist in res_artist or res_artist in req_artist:
                score += 0.3

        # Album match
        if req_album and res_album:
            if req_album == res_album:
                score += 0.4
            elif req_album in res_album or res_album in req_album:
                score += 0.3

        # Bonus for having both matches
        if score >= 0.6:
            score += 0.2

        # Base score if we got any result
        if score == 0:
            score = 0.2

        return min(score, 1.0)
