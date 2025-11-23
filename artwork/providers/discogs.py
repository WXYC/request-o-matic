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
