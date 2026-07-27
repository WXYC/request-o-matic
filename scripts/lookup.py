#!/usr/bin/env python3
"""CLI script to test the /request endpoint without posting to Slack.

Usage:
    python scripts/lookup.py "milkman aphex twin"
    python scripts/lookup.py --staging "juana molina la paradoja"
    python scripts/lookup.py --verbose "juana molina la paradoja"
"""

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path
from typing import Any, cast

# Allow running from any directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from core.server_timing import parse_server_timing
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
    context_message: str | None = None,
    result_artworks: list[dict | None] | None = None,
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

    artworks = result_artworks or []
    for i, item in enumerate(results, 1):
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
        print(f"      WXYC:     {item.get('library_url') or '(none)'}")
        item_artwork = artworks[i - 1] if i - 1 < len(artworks) else None
        discogs_url = item_artwork.get("release_url") if item_artwork else None
        print(f"      Discogs:  {discogs_url or '(none)'}")
        print()

    if artwork and artwork.get("artwork_url"):
        print_section("Artwork")
        print(f"  Image:      {artwork.get('artwork_url')}")
        if artwork.get("release_url"):
            print(f"  Discogs:    {artwork.get('release_url')}")
        print(f"  Source:     {artwork.get('source')}")
        print(f"  Confidence: {artwork.get('confidence', 0):.2f}")

        streaming_links = [
            ("Bandcamp", artwork.get("bandcamp_url")),
            ("Spotify", artwork.get("spotify_url")),
            ("Apple Music", artwork.get("apple_music_url")),
            ("YouTube Music", artwork.get("youtube_music_url")),
            ("SoundCloud", artwork.get("soundcloud_url")),
        ]
        available = [(name, url) for name, url in streaming_links if url]
        if available:
            print_section("Streaming")
            for name, url in available:
                print(f"  {name + ':':16s}{url}")


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


# Friendly labels + provenance for the stages ROM emits in its merged
# Server-Timing header. Leaves (parse, slack_post, and the forwarded LML
# sub-stages) render first; the roll-ups render after them. Unmapped stage
# names fall through to their raw form so a new stage is never silently dropped.
_STAGE_LABELS = {
    "parse": "Parse (Groq)",
    "slack_post": "Slack post",
    # LML's forwarded sub-stages (order LML emits them within its own header).
    "album_lookup": "LML: album lookup",
    "library_search": "LML: library search",
    "track_validation": "LML: track validation",
    "streaming_status": "LML: streaming status",
    "artwork_fetch": "LML: artwork fetch",
    "metadata_enrichment": "LML: metadata enrichment",
    "identity_resolution": "LML: identity resolution",
    "discogs": "LML: Discogs cache/API",
    "queue_wait": "LML: queue wait",
    "event_loop_lag": "LML: event-loop lag",
    # Roll-ups.
    "lookup_service": "LML round-trip (server)",
    "lml_wall": "LML: wall (incl. framework)",
    "lml_total": "LML: total (self-measured)",
    "total": "Server total",
}
_ROLLUP_STAGES = ("lookup_service", "lml_wall", "lml_total", "total")


def print_server_timing(header: str | None, round_trip_ms: float) -> None:
    """Print the `/request` Server-Timing breakdown, client round-trip last.

    ``header`` is the raw ``Server-Timing`` value ROM returns (rom's own stages
    merged with LML's forwarded sub-stages; see WXYC/request-o-matic#179). The
    per-stage leaves print in header order, then the roll-ups (LML round-trip,
    server total), then the client-measured ``round_trip_ms`` — so a reader sees
    where the time went (e.g. an 8.5s ``metadata_enrichment`` Apple-probe stall)
    and can reconcile it against the totals. When the server sent no header
    (older ROM, or ``ENABLE_SERVER_TIMING=false``), only the round-trip shows.
    """
    print_section("Server Timing")
    legs = parse_server_timing(header)
    if legs:
        leaves = [(name, dur) for name, dur in legs if name not in _ROLLUP_STAGES]
        rollups = [(name, dur) for name, dur in legs if name in _ROLLUP_STAGES]
        for name, dur in leaves + rollups:
            label = _STAGE_LABELS.get(name, name)
            print(f"  {label + ':':32s}{dur:8.0f} ms")
    print(f"  {'Round-trip (client):':32s}{round_trip_ms:8.0f} ms")
    if not legs:
        # Distinguish an absent header (older ROM / flag off) from a present but
        # unparseable one (every entry rejected) — different debugging signals.
        note = "no" if not header else "an unparseable"
        print(f"  (server sent {note} Server-Timing header)")


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
            start = time.perf_counter()
            response = await client.post(
                f"{base_url}/request",
                json=body,
            )
            round_trip_ms = (time.perf_counter() - start) * 1000
            response.raise_for_status()
            server_timing = response.headers.get("Server-Timing")
            data = response.json()

            # Display parsed request
            parsed = data.get("parsed", {})
            print_parsed_request(parsed)

            # Display search summary
            print_search_summary(data)

            # Skip library results for non-requests
            if not parsed.get("is_request", False):
                print_server_timing(server_timing, round_trip_ms)
                return cast(dict[str, Any], data)

            # Display library results and context
            library_results = data.get("library_results", [])
            print_library_results(
                library_results,
                data.get("artwork"),
                data.get("context_message"),
                data.get("result_artworks"),
            )

            # Display cache stats if present
            cache_stats = data.get("cache_stats")
            if cache_stats:
                print_cache_stats(cache_stats)

            # Display the server-side stage breakdown + client round-trip
            print_server_timing(server_timing, round_trip_ms)

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
  %(prog)s "milkman aphex twin"
  %(prog)s "jessica pratt on your own love again"
  %(prog)s --verbose "Play 'la paradoja' by Juana Molina"
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
