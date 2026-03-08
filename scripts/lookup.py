#!/usr/bin/env python3
"""CLI script to test the /request endpoint without posting to Slack.

Usage:
    python scripts/lookup.py "play bohemian rhapsody by queen"
    python scripts/lookup.py --staging "the beatles abbey road"
    python scripts/lookup.py --verbose "the beatles abbey road"
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any, cast

# Allow running from any directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from scripts._common import LOCAL_URL, PROD_URL, STAGING_URL, set_up_logging

logger = logging.getLogger(__name__)


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


async def lookup_discogs_urls(
    client: httpx.AsyncClient,
    base_url: str,
    results: list[dict],
) -> dict[int, str]:
    """Look up Discogs URLs for each library result.

    Returns a dict mapping item ID to Discogs release URL.
    """
    discogs_urls: dict[int, str] = {}

    async def fetch_discogs_url(item: dict) -> tuple[int, str | None]:
        """Fetch Discogs URL for a single item."""
        item_id: int = item.get("id", 0)
        artist = item.get("artist", "")
        title = item.get("title", "")

        if not artist or not title:
            return item_id, None

        try:
            response = await client.post(
                f"{base_url}/discogs/search",
                json={"artist": artist, "album": title},
                params={"limit": 1},
            )
            if response.status_code == 200:
                data = response.json()
                results_list = data.get("results", [])
                if results_list:
                    return item_id, results_list[0].get("release_url")
        except Exception:
            pass
        return item_id, None

    # Fetch all Discogs URLs concurrently
    tasks = [fetch_discogs_url(item) for item in results]
    for item_id, url in await asyncio.gather(*tasks):
        if url:
            discogs_urls[item_id] = url

    return discogs_urls


def print_search_summary(data: dict) -> None:
    """Print search summary showing what kind of results we got."""
    parsed = data.get("parsed", {})
    is_request = parsed.get("is_request", False)

    if not is_request:
        label = parsed.get("message_type", "other")
        print_section("Search")
        print(f"  Not a song request ({label}). No library search performed.")
        return

    song_not_found = data.get("song_not_found", False)
    found_on_compilation = data.get("found_on_compilation", False)
    search_type = data.get("search_type", "none")
    results = data.get("library_results", [])

    print_section("Search")
    print(f"  Strategy:  {search_type}")
    if found_on_compilation:
        print(f"  Match:     found on compilation ({len(results)} result(s))")
    elif song_not_found and results:
        print(f"  Match:     song not found -- showing other albums by artist ({len(results)})")
    elif results:
        print(f"  Match:     direct ({len(results)} result(s))")
    else:
        print("  Match:     no results")


def print_library_results(
    results: list[dict],
    artwork: dict | None,
    discogs_urls: dict[int, str] | None = None,
    context_message: str | None = None,
) -> None:
    """Print library search results."""
    print_section("Library Results")

    if context_message:
        print(f"  {context_message}")
        print()

    if not results:
        if not context_message:
            print("  No results found in library.")
        return

    discogs_urls = discogs_urls or {}

    for i, item in enumerate(results, 1):
        item_id = item.get("id")
        title = item.get("title", "")
        artist = item.get("artist", "")
        print(f"  [{i}] {artist} - {title}")
        print(f"      Album:    {title}")
        print(f"      Artist:   {artist}")
        print(f"      Genre:    {item.get('genre') or '(none)'}")
        print(f"      Format:   {item.get('format') or '(none)'}")
        call_letters = item.get("call_letters", "")
        artist_num = item.get("artist_call_number", "")
        release_num = item.get("release_call_number", "")
        if call_letters:
            print(f"      Location: {call_letters} {artist_num}/{release_num}")
        else:
            print("      Location: (none)")
        if item_id in discogs_urls:
            print(f"      Discogs:  {discogs_urls[item_id]}")
        print(f"      WXYC:     {item.get('library_url') or '(none)'}")
        print()

    if artwork and artwork.get("artwork_url"):
        print_section("Artwork")
        print(f"  Image:      {artwork.get('artwork_url')}")
        if artwork.get("release_url"):
            print(f"  Discogs:    {artwork.get('release_url')}")
        print(f"  Source:     {artwork.get('source')}")
        print(f"  Confidence: {artwork.get('confidence', 0):.2f}")


def print_cache_stats(cache_stats: dict) -> None:
    """Print Discogs cache statistics."""
    print_section("Discogs Cache")
    memory_hits = cache_stats.get("memory_hits", 0)
    memory_misses = cache_stats.get("memory_misses", 0)
    pg_hits = cache_stats.get("pg_hits", 0)
    pg_misses = cache_stats.get("pg_misses", 0)
    api_calls = cache_stats.get("api_calls", 0)
    print(f"  In-memory cache:   {memory_hits} hits, {memory_misses} misses")
    print(f"  PostgreSQL cache:  {pg_hits} hits, {pg_misses} misses")
    print(f"  Discogs API calls: {api_calls}")
    pg_time = cache_stats.get("pg_time_ms", 0)
    api_time = cache_stats.get("api_time_ms", 0)
    if pg_time > 0 or api_time > 0:
        print(f"  PG cache time:     {pg_time:.0f} ms")
        print(f"  API time:          {api_time:.0f} ms")


async def run_lookup(
    query: str,
    verbose: bool = False,
    local: bool = False,
    staging: bool = False,
    skip_cache: bool = False,
) -> dict[str, Any]:
    """Call the /request endpoint with skip_slack=true."""
    set_up_logging(verbose)
    base_url = LOCAL_URL if local else STAGING_URL if staging else PROD_URL
    logger.info(f"Processing query: {query}")
    logger.info(f"Using API: {base_url}")

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            body: dict[str, Any] = {"message": query, "skip_slack": True}
            if skip_cache:
                body["skip_cache"] = True
                logger.info("Cache bypass enabled (skip_cache=True)")
            logger.info("Calling /request endpoint...")
            response = await client.post(
                f"{base_url}/request",
                json=body,
            )
            response.raise_for_status()
            data = response.json()

            # Display parsed request
            parsed = data.get("parsed", {})
            print_parsed_request(parsed)

            # Display search summary
            print_search_summary(data)

            # Skip library results for non-requests
            if not parsed.get("is_request", False):
                return cast(dict[str, Any], data)

            # Look up Discogs URLs for each library result
            library_results = data.get("library_results", [])
            discogs_urls = {}
            if library_results:
                logger.info("Looking up Discogs URLs...")
                discogs_urls = await lookup_discogs_urls(client, base_url, library_results)

            # Display results with Discogs URLs and context
            print_library_results(
                library_results,
                data.get("artwork"),
                discogs_urls,
                data.get("context_message"),
            )

            # Display cache stats if present
            cache_stats = data.get("cache_stats")
            if cache_stats:
                print_cache_stats(cache_stats)

            return cast(dict[str, Any], data)

        except httpx.HTTPStatusError as e:
            logger.error(f"Request failed: {e.response.status_code}")
            logger.error(f"Response: {e.response.text}")
            raise SystemExit(1) from e


def main() -> None:
    """Main entry point for the CLI."""
    parser = argparse.ArgumentParser(
        description="Test the /request endpoint without posting to Slack.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s "play bohemian rhapsody by queen"
  %(prog)s "the beatles abbey road"
  %(prog)s --verbose "Play 'Abele Dance (85 Remix)' by Manu Dibango"
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
    parser.add_argument(
        "-C",
        "--skip-cache",
        action="store_true",
        help="Bypass all caches (in-memory and PG) to force API calls",
    )
    server_group = parser.add_mutually_exclusive_group()
    server_group.add_argument(
        "-l",
        "--local",
        action="store_true",
        help="Use local server (localhost:8000) instead of production",
    )
    server_group.add_argument(
        "-s",
        "--staging",
        action="store_true",
        help="Use staging server instead of production",
    )

    args = parser.parse_args()

    try:
        asyncio.run(run_lookup(args.query, args.verbose, args.local, args.staging, args.skip_cache))
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
