import logging
from typing import Optional

import httpx

from artwork.models import ArtworkRequest, SearchResult

logger = logging.getLogger(__name__)

DISCOGS_API_BASE = "https://api.discogs.com"


class DiscogsProvider:
    """Artwork provider using the Discogs API."""

    def __init__(self, token: str):
        self.token = token
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def name(self) -> str:
        return "discogs"

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=DISCOGS_API_BASE,
                headers={
                    "Authorization": f"Discogs token={self.token}",
                    "User-Agent": "RequestParserArtworkService/1.0",
                },
                timeout=10.0,
            )
        return self._client

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None

    async def search_track(self, track: str, artist: Optional[str] = None) -> Optional[str]:
        """Search Discogs for a track and return the album name that contains it."""
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
                return None

            response.raise_for_status()
            data = response.json()

            results = data.get("results", [])
            if results:
                title = results[0].get("title", "")
                _, album = self._parse_title(title)
                logger.info(f"Found album '{album}' for track '{track}'")
                return album

            return None

        except Exception as e:
            logger.error(f"Discogs track search failed: {e}")
            return None

    def _is_compilation_artist(self, artist: str) -> bool:
        """Check if artist name indicates a compilation/various artists release."""
        artist_lower = artist.lower()
        return any(
            keyword in artist_lower
            for keyword in ["various", "soundtrack", "compilation", "v/a", "v.a."]
        )

    async def _process_search_result(
        self,
        result: dict,
        track: str,
        artist: Optional[str],
        seen_albums: set,
    ) -> Optional[tuple[str, str]]:
        """
        Process a single search result, validating compilations.

        Returns (artist, album) tuple if valid, None if should be skipped.
        """
        title = result.get("title", "")
        result_artist, album = self._parse_title(title)

        if not album:
            return None

        album_key = album.lower()
        if album_key in seen_albums:
            return None

        # For Various Artists / compilations, validate the tracklist
        if artist and self._is_compilation_artist(result_artist):
            release_id = result.get("id")
            if release_id:
                is_valid = await self.validate_track_on_release(release_id, track, artist)
                if not is_valid:
                    logger.info(
                        f"Skipping '{album}' - track/artist not validated on release"
                    )
                    return None

        seen_albums.add(album_key)
        return (result_artist, album)

    async def search_releases_by_track(
        self, track: str, artist: Optional[str] = None, limit: int = 20
    ) -> list[tuple[str, str]]:
        """
        Search Discogs for ALL releases containing a track.

        Uses a hybrid approach:
        1. First search with 'track' parameter for precise tracklist matches
        2. If few results, supplement with 'q' query search to catch compilations

        For Various Artists / compilation releases, validates the tracklist
        to ensure the track by the artist actually exists on the release.

        Returns:
            List of (artist, album) tuples for releases containing the track.
        """
        client = await self._get_client()
        releases = []
        seen_albums: set = set()

        # First: precise search using track parameter
        track_params: dict = {
            "type": "release",
            "track": track,
            "per_page": limit,
        }
        if artist:
            track_params["artist"] = artist

        logger.info(f"Searching Discogs for releases with track: '{track}', artist: {artist}")

        try:
            response = await client.get("/database/search", params=track_params)

            if response.status_code != 429:
                response.raise_for_status()
                data = response.json()

                for result in data.get("results", []):
                    processed = await self._process_search_result(
                        result, track, artist, seen_albums
                    )
                    if processed:
                        releases.append(processed)

            logger.info(f"Track search found {len(releases)} releases")

            # Second: if few results, supplement with keyword search for compilations
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
                        processed = await self._process_search_result(
                            result, track, artist, seen_albums
                        )
                        if processed:
                            releases.append(processed)

                    logger.info(f"After keyword search: {len(releases)} total releases")

            return releases[:limit]

        except Exception as e:
            logger.error(f"Discogs search failed: {e}")
            return []

    async def validate_track_on_release(
        self, release_id: int, track: str, artist: str
    ) -> bool:
        """
        Fetch release details and verify the track/artist actually exist.

        Args:
            release_id: Discogs release ID
            track: Track name to find
            artist: Artist name to find

        Returns:
            True if the track by the artist is found on the release
        """
        client = await self._get_client()
        track_lower = track.lower()
        artist_lower = artist.lower()

        try:
            response = await client.get(f"/releases/{release_id}")

            if response.status_code == 429:
                logger.warning("Discogs rate limit hit during tracklist fetch")
                return False

            response.raise_for_status()
            data = response.json()

            tracklist = data.get("tracklist", [])
            for item in tracklist:
                item_title = (item.get("title") or "").lower()
                # Check if track title matches (exact or contained)
                if track_lower not in item_title and item_title not in track_lower:
                    continue

                # Check if any artist on this track matches
                track_artists = item.get("artists", [])
                if not track_artists:
                    # If no per-track artists, this might be a single-artist release
                    # Check the release-level artists
                    track_artists = data.get("artists", [])

                for track_artist in track_artists:
                    artist_name = (track_artist.get("name") or "").lower()
                    # Remove Discogs numbering like "(2)" from artist names
                    artist_name = artist_name.split("(")[0].strip()
                    if artist_lower in artist_name or artist_name in artist_lower:
                        logger.info(
                            f"Validated: '{track}' by '{artist}' found on release {release_id}"
                        )
                        return True

            logger.info(
                f"Track '{track}' by '{artist}' NOT found on release {release_id}"
            )
            return False

        except Exception as e:
            logger.warning(f"Failed to validate release {release_id}: {e}")
            return False

    async def search(self, request: ArtworkRequest) -> list[SearchResult]:
        """Search Discogs for album artwork."""
        params = self._build_search_params(request)
        if not params:
            logger.warning("No searchable fields in request")
            return []

        logger.info(f"Searching Discogs with params: {params}")
        client = await self._get_client()

        try:
            response = await client.get(
                "/database/search",
                params=params,
            )

            if response.status_code == 429:
                logger.warning("Discogs rate limit hit")
                return []

            response.raise_for_status()
            data = response.json()

            # If strict search returned nothing, try a fuzzy query search
            if not data.get("results") and (request.artist or request.album):
                query_parts = []
                if request.artist:
                    query_parts.append(request.artist)
                if request.album:
                    query_parts.append(request.album)
                fallback_params = {
                    "type": "release",
                    "per_page": 5,
                    "q": " ".join(query_parts),
                }
                logger.info(f"Strict search empty, trying fuzzy query: {fallback_params}")
                response = await client.get("/database/search", params=fallback_params)
                response.raise_for_status()
                data = response.json()

            results = []
            for item in data.get("results", []):
                # Search results return 'thumb' for thumbnail URL
                cover_url = item.get("thumb")
                if not cover_url or "spacer.gif" in cover_url:
                    continue

                title = item.get("title", "")
                # Discogs format is "Artist - Album"
                artist, album = self._parse_title(title)

                confidence = self._calculate_confidence(request, artist, album)

                # Construct release URL from id and type
                release_id = item.get("id")
                release_type = item.get("type", "release")
                release_url = f"https://www.discogs.com/{release_type}/{release_id}"

                results.append(
                    SearchResult(
                        artwork_url=cover_url,
                        release_url=release_url,
                        album=album,
                        artist=artist,
                        source=self.name,
                        confidence=confidence,
                    )
                )

            results.sort(key=lambda r: r.confidence, reverse=True)
            return results

        except httpx.HTTPStatusError as e:
            logger.error(f"Discogs API error: {e.response.status_code}")
            return []
        except Exception as e:
            logger.error(f"Discogs search failed: {e}")
            return []

    def _build_search_params(self, request: ArtworkRequest) -> dict:
        """Build search params using Discogs-specific fields for better results."""
        params: dict = {
            "type": "release",
            "per_page": 5,
        }

        # Use specific search fields when available for better matching
        if request.artist:
            params["artist"] = request.artist
        if request.album:
            params["release_title"] = request.album
        elif request.song:
            # Fall back to song title in release_title if no album
            params["release_title"] = request.song

        # If we only have general info, use the query param
        if "artist" not in params and "release_title" not in params:
            return {}

        return params

    def _parse_title(self, title: str) -> tuple[str, str]:
        """Parse Discogs title format 'Artist - Album' into components."""
        if " - " in title:
            parts = title.split(" - ", 1)
            return parts[0].strip(), parts[1].strip()
        return "", title

    def _calculate_confidence(
        self, request: ArtworkRequest, result_artist: str, result_album: str
    ) -> float:
        """Calculate confidence score based on how well result matches request."""
        score = 0.0

        # Normalize strings for comparison
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
