"""Discogs API service with caching."""
import logging
from typing import Any, Optional

import httpx

from core.matching import calculate_confidence, is_compilation_artist
from discogs.cache import RELEASE_CACHE, SEARCH_CACHE, TRACK_CACHE, async_cached
from discogs.models import (
    DiscogsSearchRequest,
    DiscogsSearchResponse,
    DiscogsSearchResult,
    ReleaseInfo,
    ReleaseMetadataResponse,
    TrackAlbumResponse,
    TrackItem,
    TrackReleasesResponse,
)

logger = logging.getLogger(__name__)

DISCOGS_API_BASE = "https://api.discogs.com"


class DiscogsService:
    """Service for all Discogs API interactions with caching."""

    def __init__(self, token: str):
        """Initialize the service with a Discogs API token.

        Args:
            token: Discogs API token
        """
        self.token = token
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=DISCOGS_API_BASE,
                headers={
                    "Authorization": f"Discogs token={self.token}",
                    "User-Agent": "RequestParserDiscogsService/1.0",
                },
                timeout=10.0,
            )
        return self._client

    async def close(self):
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    def _parse_title(self, title: str) -> tuple[str, str]:
        """Parse Discogs title format 'Artist - Album' into components."""
        if " - " in title:
            parts = title.split(" - ", 1)
            return parts[0].strip(), parts[1].strip()
        return "", title

    @async_cached(TRACK_CACHE)
    async def search_track(
        self, track: str, artist: Optional[str] = None
    ) -> TrackAlbumResponse:
        """Search for a track and return the album that contains it.

        Args:
            track: Track title to search for
            artist: Optional artist name for filtering

        Returns:
            TrackAlbumResponse with album info if found
        """
        params: dict = {
            "type": "release",
            "track": track,
            "per_page": 5,
        }
        if artist:
            params["artist"] = artist

        logger.info(f"Searching Discogs for track: {track}, artist: {artist}")
        client = await self._get_client()

        try:
            response = await client.get("/database/search", params=params)

            if response.status_code == 429:
                logger.warning("Discogs rate limit hit")
                return TrackAlbumResponse(cached=False)

            response.raise_for_status()
            data = response.json()

            results = data.get("results", [])
            if results:
                result = results[0]
                title = result.get("title", "")
                result_artist, album = self._parse_title(title)
                release_id = result.get("id")
                release_url = f"https://www.discogs.com/release/{release_id}"

                logger.info(f"Found album '{album}' for track '{track}'")
                return TrackAlbumResponse(
                    album=album,
                    artist=result_artist,
                    release_id=release_id,
                    release_url=release_url,
                    cached=False,
                )

            return TrackAlbumResponse(cached=False)

        except Exception as e:
            logger.error(f"Discogs track search failed: {e}")
            return TrackAlbumResponse(cached=False)

    @async_cached(TRACK_CACHE)
    async def search_releases_by_track(
        self, track: str, artist: Optional[str] = None, limit: int = 20
    ) -> TrackReleasesResponse:
        """Search for ALL releases containing a track.

        Uses a hybrid approach:
        1. First search with 'track' parameter for precise matches
        2. If few results, supplement with keyword search for compilations

        Args:
            track: Track title to search for
            artist: Optional artist name for filtering
            limit: Maximum number of results

        Returns:
            TrackReleasesResponse with list of releases
        """
        client = await self._get_client()
        releases: list[ReleaseInfo] = []
        seen_albums: set = set()

        params: dict = {
            "type": "release",
            "track": track,
            "per_page": limit,
        }
        if artist:
            params["artist"] = artist

        logger.info(f"Searching Discogs for releases with track: '{track}', artist: {artist}")

        try:
            response = await client.get("/database/search", params=params)

            if response.status_code != 429:
                response.raise_for_status()
                data = response.json()

                for result in data.get("results", []):
                    release_info = self._process_search_result(result, seen_albums)
                    if release_info:
                        releases.append(release_info)

            logger.info(f"Track search found {len(releases)} releases")

            # Supplement with keyword search if few results
            if len(releases) < 3:
                query_parts = [track]
                if artist:
                    query_parts.append(artist)

                query_params: dict = {
                    "type": "release",
                    "q": " ".join(query_parts),
                    "per_page": limit,
                }

                logger.info(f"Supplementing with keyword search: '{query_params['q']}'")
                response = await client.get("/database/search", params=query_params)

                if response.status_code != 429:
                    response.raise_for_status()
                    data = response.json()

                    for result in data.get("results", []):
                        release_info = self._process_search_result(result, seen_albums)
                        if release_info:
                            releases.append(release_info)

                    logger.info(f"After keyword search: {len(releases)} total releases")

            return TrackReleasesResponse(
                track=track,
                artist=artist,
                releases=releases[:limit],
                total=len(releases[:limit]),
                cached=False,
            )

        except Exception as e:
            logger.error(f"Discogs search failed: {e}")
            return TrackReleasesResponse(track=track, artist=artist, cached=False)

    def _process_search_result(
        self, result: dict, seen_albums: set
    ) -> Optional[ReleaseInfo]:
        """Process a single search result into a ReleaseInfo.

        Args:
            result: Raw Discogs API result
            seen_albums: Set of already-seen album titles (for deduplication)

        Returns:
            ReleaseInfo if valid, None if should be skipped
        """
        title = result.get("title", "")
        result_artist, album = self._parse_title(title)

        if not album:
            return None

        album_key = album.lower()
        if album_key in seen_albums:
            return None

        seen_albums.add(album_key)

        release_id = result.get("id")
        if release_id is None:
            return None

        is_compilation = is_compilation_artist(result_artist)

        return ReleaseInfo(
            album=album,
            artist=result_artist,
            release_id=release_id,
            release_url=f"https://www.discogs.com/release/{release_id}",
            is_compilation=is_compilation,
        )

    @async_cached(RELEASE_CACHE)
    async def get_release(self, release_id: int) -> Optional[ReleaseMetadataResponse]:
        """Get full release metadata by ID.

        Args:
            release_id: Discogs release ID

        Returns:
            ReleaseMetadataResponse with full metadata, or None on error
        """
        client = await self._get_client()

        try:
            response = await client.get(f"/releases/{release_id}")

            if response.status_code == 429:
                logger.warning("Discogs rate limit hit")
                return None

            response.raise_for_status()
            data = response.json()

            # Extract artists
            artists = data.get("artists", [])
            artist_name = artists[0].get("name", "") if artists else ""

            # Extract labels
            labels = data.get("labels", [])
            label_name = labels[0].get("name") if labels else None

            # Extract tracklist with per-track artists (for compilations)
            tracklist = [
                TrackItem(
                    position=t.get("position", ""),
                    title=t.get("title", ""),
                    duration=t.get("duration"),
                    artists=[a.get("name", "") for a in t.get("artists", [])],
                )
                for t in data.get("tracklist", [])
            ]

            # Extract artwork
            images = data.get("images", [])
            artwork_url = images[0].get("uri") if images else None

            return ReleaseMetadataResponse(
                release_id=release_id,
                title=data.get("title", ""),
                artist=artist_name,
                year=data.get("year"),
                label=label_name,
                genres=data.get("genres", []),
                styles=data.get("styles", []),
                tracklist=tracklist,
                artwork_url=artwork_url,
                release_url=f"https://www.discogs.com/release/{release_id}",
                cached=False,
            )

        except Exception as e:
            logger.error(f"Failed to fetch release {release_id}: {e}")
            return None

    @async_cached(SEARCH_CACHE)
    async def search(
        self, request: DiscogsSearchRequest, limit: int = 5
    ) -> DiscogsSearchResponse:
        """General release search for artwork discovery.

        Args:
            request: Search parameters (artist, album, track)
            limit: Maximum number of results to return

        Returns:
            DiscogsSearchResponse with ranked results
        """
        params = self._build_search_params(request, limit=limit)
        if not params:
            logger.warning("No searchable fields in request")
            return DiscogsSearchResponse(cached=False)

        logger.info(f"Searching Discogs with params: {params}")
        client = await self._get_client()

        try:
            response = await client.get("/database/search", params=params)

            if response.status_code == 429:
                logger.warning("Discogs rate limit hit")
                return DiscogsSearchResponse(cached=False)

            response.raise_for_status()
            data = response.json()

            # If strict search returned nothing, try fuzzy query
            if not data.get("results") and (request.artist or request.album):
                query_parts = []
                if request.artist:
                    query_parts.append(request.artist)
                if request.album:
                    query_parts.append(request.album)
                fallback_params: dict[str, Any] = {
                    "type": "release",
                    "per_page": limit,
                    "q": " ".join(query_parts),
                }
                logger.info(f"Strict search empty, trying fuzzy query: {fallback_params}")
                response = await client.get("/database/search", params=fallback_params)
                response.raise_for_status()
                data = response.json()

            results = []
            for item in data.get("results", []):
                cover_url = item.get("thumb")
                if not cover_url or "spacer.gif" in cover_url:
                    cover_url = None

                title = item.get("title", "")
                result_artist, album = self._parse_title(title)

                confidence = calculate_confidence(
                    request.artist, request.album, result_artist, album
                )

                release_id = item.get("id")
                release_url = f"https://www.discogs.com/release/{release_id}"

                results.append(
                    DiscogsSearchResult(
                        album=album,
                        artist=result_artist,
                        release_id=release_id,
                        release_url=release_url,
                        artwork_url=cover_url,
                        confidence=confidence,
                    )
                )

            results.sort(key=lambda r: r.confidence, reverse=True)

            return DiscogsSearchResponse(
                results=results,
                total=len(results),
                cached=False,
            )

        except Exception as e:
            logger.error(f"Discogs search failed: {e}")
            return DiscogsSearchResponse(cached=False)

    def _build_search_params(self, request: DiscogsSearchRequest, limit: int = 5) -> dict:
        """Build search params using Discogs-specific fields.

        Args:
            request: Search request with artist/album/track
            limit: Maximum number of results to return

        Returns:
            Dict of search parameters, or empty dict if no searchable fields
        """
        params: dict = {
            "type": "release",
            "per_page": limit,
        }

        if request.artist:
            params["artist"] = request.artist
        if request.album:
            params["release_title"] = request.album
        elif request.track:
            params["release_title"] = request.track

        if "artist" not in params and "release_title" not in params:
            return {}

        return params

    async def validate_track_on_release(
        self, release_id: int, track: str, artist: str
    ) -> bool:
        """Validate that a track by an artist exists on a release.

        Args:
            release_id: Discogs release ID
            track: Track title to find
            artist: Artist name to find

        Returns:
            True if the track by the artist is found on the release
        """
        release = await self.get_release(release_id)
        if release is None:
            return False

        track_lower = track.lower()
        artist_lower = artist.lower()

        for item in release.tracklist:
            item_title = item.title.lower()
            # Check if track title matches
            if track_lower not in item_title and item_title not in track_lower:
                continue

            # Check per-track artists first (for compilations)
            if item.artists:
                for track_artist in item.artists:
                    track_artist_lower = track_artist.lower().split("(")[0].strip()
                    if artist_lower in track_artist_lower or track_artist_lower in artist_lower:
                        logger.info(
                            f"Validated: '{track}' by '{artist}' found on release {release_id}"
                        )
                        return True
            else:
                # For single-artist releases, check release artist
                release_artist = release.artist.lower()
                # Remove Discogs numbering like "(2)"
                release_artist = release_artist.split("(")[0].strip()

                if artist_lower in release_artist or release_artist in artist_lower:
                    logger.info(
                        f"Validated: '{track}' by '{artist}' found on release {release_id}"
                    )
                    return True

        logger.info(f"Track '{track}' by '{artist}' NOT found on release {release_id}")
        return False
