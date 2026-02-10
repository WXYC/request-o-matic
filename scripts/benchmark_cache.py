#!/usr/bin/env python3
"""Benchmark PG cache vs Discogs API response times for search().

Runs a set of representative queries through both code paths and compares timing.

Usage:
    # Requires DATABASE_URL_DISCOGS and DISCOGS_TOKEN env vars
    venv/bin/python scripts/benchmark_cache.py --iterations 3

    # Use Railway staging environment
    railway run -- venv/bin/python scripts/benchmark_cache.py
"""

import argparse
import asyncio
import logging
import os
import sys
import time

import asyncpg

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from discogs.cache_service import DiscogsCacheService
from discogs.memory_cache import clear_all_caches
from discogs.models import DiscogsSearchRequest
from discogs.ratelimit import reset_rate_limiting
from discogs.service import DiscogsService

logger = logging.getLogger(__name__)

# Representative queries covering all three callers of search()
BENCHMARK_QUERIES: list[dict[str, str | None]] = [
    {
        "label": "Radiohead / OK Computer",
        "caller": "artwork",
        "artist": "Radiohead",
        "album": "OK Computer",
    },
    {
        "label": "Lush / Split",
        "caller": "artwork",
        "artist": "Lush",
        "album": "Split",
    },
    {
        "label": "Portishead / Dummy",
        "caller": "track_validation",
        "artist": "Portishead",
        "album": "Dummy",
    },
    {
        "label": "Bjork (artist-only)",
        "caller": "lookup_by_artist",
        "artist": "Bjork",
        "album": None,
    },
    {
        "label": "Radiohead (artist-only)",
        "caller": "lookup_by_artist",
        "artist": "Radiohead",
        "album": None,
    },
    {
        "label": "Autechre / Confield",
        "caller": "artwork",
        "artist": "Autechre",
        "album": "Confield",
    },
]


async def time_search(service: DiscogsService, query: dict[str, str | None]) -> tuple[float, bool]:
    """Run a single search query and return (time_ms, cached).

    Returns:
        Tuple of (elapsed_ms, was_cached)
    """
    request = DiscogsSearchRequest(artist=query["artist"], album=query.get("album"))

    start = time.perf_counter()
    result = await service.search(request)
    elapsed_ms = (time.perf_counter() - start) * 1000

    return elapsed_ms, result.cached


async def run_benchmark(iterations: int) -> None:
    """Run the full benchmark suite."""
    db_url = os.environ.get("DATABASE_URL_DISCOGS")
    token = os.environ.get("DISCOGS_TOKEN")

    if not token:
        print("ERROR: DISCOGS_TOKEN environment variable is required")
        sys.exit(1)

    # Set up services
    pool = None
    cache_service = None
    if db_url:
        try:
            pool = await asyncpg.create_pool(db_url, min_size=1, max_size=3)
            cache_service = DiscogsCacheService(pool)
            available = await cache_service.is_available()
            if available:
                print(f"  PG cache connected: {db_url[:40]}...")
            else:
                print("  WARNING: PG cache connected but health check failed")
                cache_service = None
        except Exception as e:
            print(f"  WARNING: Could not connect to PG cache: {e}")
    else:
        print("  DATABASE_URL_DISCOGS not set - cache path will be skipped")

    service_with_cache = DiscogsService(token, cache_service=cache_service)
    service_api_only = DiscogsService(token, cache_service=None)

    print(f"\n  Running {len(BENCHMARK_QUERIES)} queries x {iterations} iterations\n")
    print(
        f"  {'Query':<30} {'Caller':<20} "
        f"{'Cache (ms)':>12} {'API (ms)':>12} {'Speedup':>10} {'Cache Hit':>10}"
    )
    print("  " + "-" * 96)

    total_cache_ms = 0.0
    total_api_ms = 0.0
    cache_hits = 0
    total_queries = 0

    for query in BENCHMARK_QUERIES:
        cache_times = []
        api_times = []
        was_cached = False

        for _ in range(iterations):
            # Clear in-memory caches between runs so we measure PG/API, not memory cache
            clear_all_caches()
            reset_rate_limiting()

            # Run with cache (PG + API fallback)
            if cache_service:
                cache_ms, was_cached = await time_search(service_with_cache, query)
                cache_times.append(cache_ms)

            # Clear in-memory caches again for fair API comparison
            clear_all_caches()
            reset_rate_limiting()

            # Run API-only
            api_ms, _ = await time_search(service_api_only, query)
            api_times.append(api_ms)

        avg_cache = sum(cache_times) / len(cache_times) if cache_times else float("nan")
        avg_api = sum(api_times) / len(api_times) if api_times else float("nan")

        if cache_times and avg_cache > 0:
            speedup = f"{avg_api / avg_cache:.1f}x"
        else:
            speedup = "N/A"

        hit_str = "yes" if was_cached else "no"
        cache_str = f"{avg_cache:.0f}" if cache_times else "N/A"

        print(
            f"  {query['label']:<30} {query['caller']:<20} "
            f"{cache_str:>12} {avg_api:>12.0f} {speedup:>10} {hit_str:>10}"
        )

        if cache_times:
            total_cache_ms += avg_cache
            if was_cached:
                cache_hits += 1
        total_api_ms += avg_api
        total_queries += 1

    # Summary
    print("  " + "-" * 96)
    if cache_service:
        avg_cache_total = total_cache_ms / total_queries
        avg_api_total = total_api_ms / total_queries
        overall_speedup = avg_api_total / avg_cache_total if avg_cache_total > 0 else float("inf")
        print(
            f"  {'AVERAGE':<30} {'':<20} "
            f"{avg_cache_total:>12.0f} {avg_api_total:>12.0f} "
            f"{overall_speedup:>9.1f}x {cache_hits}/{total_queries}{'':>5}"
        )
    else:
        avg_api_total = total_api_ms / total_queries
        print(f"  {'AVERAGE (API only)':<30} {'':<20} {'N/A':>12} {avg_api_total:>12.0f}")

    print()

    # Clean up
    await service_with_cache.close()
    await service_api_only.close()
    if pool:
        await pool.close()


def main():
    parser = argparse.ArgumentParser(description="Benchmark PG cache vs Discogs API for search()")
    parser.add_argument(
        "--iterations",
        type=int,
        default=3,
        help="Number of iterations per query (default: 3)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    print("\n  Discogs Cache Benchmark: search()")
    print("  " + "=" * 40)

    asyncio.run(run_benchmark(args.iterations))


if __name__ == "__main__":
    main()
