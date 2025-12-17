#!/usr/bin/env python3
"""CLI script to test parsing and album lookup strategies via production API.

Replicates the full /request endpoint flow without posting to Slack:
1. Parse the message
2. Look up album from track (if song provided but no album)
3. Search library with artist + album
4. Fallback to artist-only search
5. Search for compilations if song not found
6. Fetch artwork for results

Usage:
    python scripts/lookup.py "play bohemian rhapsody by queen"
    python scripts/lookup.py --verbose "the beatles abbey road"
"""
import argparse
import asyncio
import logging
import os
import re
import sys
from typing import Optional

import httpx
from dotenv import load_dotenv

# Load .env file from project root
load_dotenv()

logger = logging.getLogger(__name__)

BASE_URL = "https://request-o-matic-production.up.railway.app/api/v1"
DISCOGS_API_BASE = "https://api.discogs.com"


def set_up_logging(verbose: bool) -> None:
    """Configure logging based on verbosity level."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler()],
    )
    if not verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)


def print_section(title: str) -> None:
    """Print a section header."""
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def print_parsed_request(parsed: dict) -> None:
    """Print parsed request details."""
    print_section("Parsed Request")
    print(f"  Is Request:    {parsed.get('is_request')}")
    print(f"  Message Type:  {parsed.get('message_type')}")
    print(f"  Artist:        {parsed.get('artist') or '(none)'}")
    print(f"  Album:         {parsed.get('album') or '(none)'}")
    print(f"  Song:          {parsed.get('song') or '(none)'}")
    print(f"  Raw Message:   {parsed.get('raw_message')}")


def print_library_results(
    results: list[dict],
    context: Optional[str],
    artwork_map: dict[int, Optional[dict]],
) -> None:
    """Print library search results with artwork info."""
    print_section("Library Results")

    if context:
        print(f"  Context: {context}")
        print()

    if not results:
        print("  No results found in library.")
        return

    for i, item in enumerate(results, 1):
        print(f"  [{i}] {item.get('title')}")
        print(f"      Artist:   {item.get('artist')}")
        print(f"      Label:    {item.get('label') or '(none)'}")
        print(f"      Format:   {item.get('format') or '(none)'}")
        print(f"      Genre:    {item.get('genre') or '(none)'}")
        print(f"      Location: {item.get('location') or '(none)'}")

        artwork = artwork_map.get(item.get("id"))
        if artwork and artwork.get("url"):
            print(f"      Artwork:  {artwork['url']}")
            confidence = artwork.get("confidence", 0)
            print(f"      Source:   {artwork.get('source')} (confidence: {confidence:.2f})")
        else:
            print("      Artwork:  (none found)")
        print()


class DiscogsClient:
    """Simple Discogs API client for track/release lookups."""

    def __init__(self, token: str):
        self.token = token
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=DISCOGS_API_BASE,
                headers={
                    "Authorization": f"Discogs token={self.token}",
                    "User-Agent": "RequestParserLookupScript/1.0",
                },
                timeout=10.0,
            )
        return self._client

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None

    async def lookup_album_by_track(
        self, track: str, artist: Optional[str] = None
    ) -> Optional[str]:
        """Search Discogs for a track and return the album name."""
        params: dict = {
            "type": "release",
            "track": track,
            "per_page": 5,
        }
        if artist:
            params["artist"] = artist

        logger.info(f"Discogs: Looking up album for track '{track}' by '{artist}'")
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
                if " - " in title:
                    _, album = title.split(" - ", 1)
                    logger.info(f"Discogs: Found album '{album.strip()}'")
                    return album.strip()
            return None
        except Exception as e:
            logger.error(f"Discogs track lookup failed: {e}")
            return None

    async def search_releases_by_track(
        self, track: str, artist: Optional[str] = None, limit: int = 20
    ) -> list[tuple[str, str]]:
        """Search for ALL releases containing a track (for compilation search)."""
        query_parts = []
        if track:
            query_parts.append(track)
        if artist:
            query_parts.append(artist)

        params: dict = {
            "type": "release",
            "q": " ".join(query_parts),
            "per_page": limit,
        }

        logger.info(f"Discogs: Searching all releases for '{params['q']}'")
        client = await self._get_client()

        try:
            response = await client.get("/database/search", params=params)
            if response.status_code == 429:
                logger.warning("Discogs rate limit hit")
                return []
            response.raise_for_status()
            data = response.json()

            releases = []
            for result in data.get("results", []):
                title = result.get("title", "")
                if " - " in title:
                    result_artist, album = title.split(" - ", 1)
                    releases.append((result_artist.strip(), album.strip()))

            logger.info(f"Discogs: Found {len(releases)} releases")
            return releases
        except Exception as e:
            logger.error(f"Discogs search failed: {e}")
            return []


async def parse_message(client: httpx.AsyncClient, message: str) -> dict:
    """Call the parse endpoint."""
    logger.info("API: Calling /parse endpoint...")
    response = await client.post(f"{BASE_URL}/parse", json={"message": message})
    response.raise_for_status()
    return response.json()


async def search_library(
    client: httpx.AsyncClient, query: str, limit: int = 5
) -> list[dict]:
    """Call the library search endpoint."""
    logger.info(f"API: Searching library for '{query}'")
    params = {"q": query, "limit": limit}
    response = await client.get(f"{BASE_URL}/library/search", params=params)
    response.raise_for_status()
    data = response.json()
    results = data.get("results", [])
    logger.info(f"API: Found {len(results)} library results")
    return results


def filter_results_by_artist(results: list[dict], artist: str) -> list[dict]:
    """Filter library results to only include those matching the artist.

    Checks if the searched artist name appears in the result's artist field.
    This helps filter out false positives from fuzzy search.
    """
    if not artist:
        return results

    artist_lower = artist.lower()
    filtered = []
    for item in results:
        item_artist = (item.get("artist") or "").lower()
        # Check if searched artist is in the result's artist field
        if artist_lower in item_artist:
            filtered.append(item)

    return filtered


async def get_artwork(
    client: httpx.AsyncClient, artist: Optional[str], album: Optional[str]
) -> Optional[dict]:
    """Call the artwork endpoint."""
    if not artist and not album:
        return None
    try:
        response = await client.post(
            f"{BASE_URL}/artwork", json={"artist": artist, "album": album}
        )
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 503:
            logger.debug("Artwork service unavailable")
        else:
            logger.warning(f"Artwork lookup failed: {e}")
        return None


async def search_album_fuzzy(
    client: httpx.AsyncClient, album_title: str
) -> list[dict]:
    """Search library by album title with fuzzy matching."""
    # Try exact search first
    results = await search_library(client, album_title, limit=1)
    if results:
        return results

    # Extract significant keywords for fuzzy search
    words = re.sub(r"[^\w\s]", " ", album_title.lower()).split()
    significant_words = [
        w
        for w in words
        if len(w) > 3
        and w not in {"the", "and", "with", "from", "that", "this", "story", "records"}
    ]

    if significant_words:
        fuzzy_query = " ".join(significant_words[:4])
        logger.info(f"Trying fuzzy album search: '{fuzzy_query}'")
        results = await search_library(client, fuzzy_query, limit=3)

        # Filter to results containing at least 2 keywords
        if results:
            filtered = []
            for result in results:
                title_lower = (result.get("title") or "").lower()
                matches = sum(1 for word in significant_words if word in title_lower)
                if matches >= 2:
                    filtered.append(result)
            return filtered

    return []


async def search_compilations(
    http_client: httpx.AsyncClient,
    discogs: Optional[DiscogsClient],
    parsed: dict,
) -> tuple[list[dict], dict[int, str]]:
    """Search for track on compilation albums using Discogs."""
    song = parsed.get("song")
    artist = parsed.get("artist")

    if not song or not artist or not discogs:
        return [], {}

    print_section("Compilation Search")
    print(f"  Searching for '{song}' by {artist} on other releases...")

    results = []
    seen_ids = set()
    discogs_titles: dict[int, str] = {}

    # Search Discogs for all releases containing this track
    releases = await discogs.search_releases_by_track(song, artist)
    print(f"  Found {len(releases)} releases on Discogs")

    # Check each release against our library
    for release_artist, release_album in releases:
        # Skip if album name matches artist name (Discogs artifact)
        if artist and release_album.lower().strip() == artist.lower().strip():
            continue
        # Skip very short album titles
        if len(release_album.strip()) < 3:
            continue

        matches = await search_album_fuzzy(http_client, release_album)
        if matches:
            print(f"  ✓ Found in library: '{matches[0].get('title')}'")
            for match in matches:
                item_id = match.get("id")
                if item_id not in seen_ids:
                    results.append(match)
                    seen_ids.add(item_id)
                    discogs_titles[item_id] = release_album

            if len(results) >= 5:
                break

    return results[:5], discogs_titles


async def run_lookup(query: str, verbose: bool = False) -> dict:
    """Run the full lookup pipeline on a query string.

    Replicates the server-side /request flow without Slack posting.
    """
    set_up_logging(verbose)
    logger.info(f"Processing query: {query}")
    logger.info(f"Using API: {BASE_URL}")

    # Check for Discogs token
    discogs_token = os.environ.get("DISCOGS_TOKEN")
    discogs: Optional[DiscogsClient] = None
    if discogs_token:
        discogs = DiscogsClient(discogs_token)
        logger.info("Discogs token found - full lookup enabled")
    else:
        logger.warning("DISCOGS_TOKEN not set - track/compilation lookup disabled")

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            # Step 1: Parse the message
            try:
                parsed = await parse_message(client, query)
                print_parsed_request(parsed)
            except httpx.HTTPStatusError as e:
                logger.error(f"Parse failed: {e.response.status_code} - {e.response.text}")
                raise SystemExit(1)

            library_results: list[dict] = []
            song_not_found = False
            found_on_compilation = False
            context: Optional[str] = None
            discogs_titles: dict[int, str] = {}

            # Step 2: If we have a song but no album, look up the album from Discogs
            album_for_search = parsed.get("album")
            if parsed.get("song") and not album_for_search and discogs:
                print_section("Album Lookup")
                album_from_track = await discogs.lookup_album_by_track(
                    parsed["song"], parsed.get("artist")
                )
                if album_from_track:
                    print(f"  Found album: '{album_from_track}'")
                    album_for_search = album_from_track
                else:
                    print("  Could not find album for this track")
                    song_not_found = True

            # Step 3: Search library with artist + album
            query_parts = []
            if parsed.get("artist"):
                query_parts.append(parsed["artist"])
            if album_for_search:
                query_parts.append(album_for_search)

            if query_parts:
                search_query = " ".join(query_parts)
                print_section("Library Search")
                print(f"  Query: {search_query}")

                library_results = await search_library(client, search_query)
                print(f"  Found {len(library_results)} results")
                for item in library_results:
                    print(f"    - {item.get('artist')} / {item.get('title')}")

                # Filter to results that actually match the artist
                if parsed.get("artist"):
                    filtered = filter_results_by_artist(library_results, parsed["artist"])
                    if len(filtered) < len(library_results):
                        print(f"  Filtered to {len(filtered)} results matching artist '{parsed['artist']}'")
                    library_results = filtered

                # Step 4: Fallback to artist-only if needed
                if not library_results and parsed.get("artist") and album_for_search:
                    print(f"\n  No results, trying artist only: {parsed['artist']}")
                    library_results = await search_library(client, parsed["artist"])
                    print(f"  Found {len(library_results)} results")
                    for item in library_results:
                        print(f"    - {item.get('artist')} / {item.get('title')}")
                    # Filter to results that actually match the artist
                    filtered = filter_results_by_artist(library_results, parsed["artist"])
                    if len(filtered) < len(library_results):
                        print(f"  Filtered to {len(filtered)} results matching artist '{parsed['artist']}'")
                    library_results = filtered
                    if library_results:
                        song_not_found = True
            else:
                # No artist/album extracted - try searching with raw message
                # This handles cases like "disco not disco" which is an album title
                raw_msg = parsed.get("raw_message", "").strip()
                if raw_msg:
                    print_section("Library Search")
                    print(f"  No artist/album parsed, searching raw query: {raw_msg}")
                    library_results = await search_library(client, raw_msg)
                    print(f"  Found {len(library_results)} results")
                    for item in library_results:
                        print(f"    - {item.get('artist')} / {item.get('title')}")

            # Step 5: Search compilations if exact song/album not found
            if song_not_found and parsed.get("song") and parsed.get("artist"):
                compilation_results, discogs_titles = await search_compilations(
                    client, discogs, parsed
                )
                if compilation_results:
                    library_results = compilation_results
                    found_on_compilation = True
                    song_not_found = False

            # Step 6: Fetch artwork for each result
            artwork_map: dict[int, Optional[dict]] = {}
            if library_results:
                print_section("Artwork Lookup")
                for item in library_results:
                    item_id = item.get("id")
                    artist = item.get("artist")
                    # Use Discogs album title if we have it (from compilation search)
                    album = discogs_titles.get(item_id, item.get("title"))

                    # Handle "Various Artists" compilations
                    if artist and artist.lower().startswith("various artists"):
                        artist = "Various"

                    artwork = await get_artwork(client, artist, album)
                    artwork_map[item_id] = artwork

                    album_display = (album or "")[:40]
                    if artwork and artwork.get("url"):
                        print(f"  ✓ {album_display}: found")
                    else:
                        print(f"  ✗ {album_display}: no artwork")

            # Build context message
            if found_on_compilation:
                context = f"Found \"{parsed.get('song')}\" by {parsed.get('artist')} on:"
            elif song_not_found and library_results:
                # Only show "here are other albums" if we actually have results
                if parsed.get("song") and parsed.get("album"):
                    context = f"\"{parsed['album']}\" not found, but here are other albums by {parsed.get('artist')}:"
                elif parsed.get("song"):
                    context = f"\"{parsed['song']}\" not found on any album, but here are some albums by {parsed.get('artist')}:"
            elif song_not_found and not library_results:
                # No results at all
                if parsed.get("song"):
                    context = f"\"{parsed['song']}\" by {parsed.get('artist')} not found in library."

            # Print final results
            print_library_results(library_results, context, artwork_map)

            return {
                "parsed": parsed,
                "library_results": library_results,
                "artwork_map": artwork_map,
                "context": context,
                "found_on_compilation": found_on_compilation,
                "song_not_found": song_not_found,
            }

        finally:
            if discogs:
                await discogs.close()


def main() -> None:
    """Main entry point for the CLI."""
    parser = argparse.ArgumentParser(
        description="Test request parsing and album lookup via production API (no Slack).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s "play bohemian rhapsody by queen"
  %(prog)s "the beatles abbey road"
  %(prog)s --verbose "Play 'Abele Dance (85 Remix)' by Manu Dibango"

Environment Variables:
  DISCOGS_TOKEN    Enables track lookup and compilation search
        """,
    )
    parser.add_argument(
        "query",
        help="The query string to parse and look up",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose/debug logging",
    )

    args = parser.parse_args()

    try:
        asyncio.run(run_lookup(args.query, args.verbose))
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Error: {e}")
        if args.verbose:
            import traceback

            traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
