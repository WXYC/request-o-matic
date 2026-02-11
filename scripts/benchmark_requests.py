#!/usr/bin/env python3
"""Benchmark end-to-end request latency: cached vs uncached (skip_cache).

Sends representative queries through the /request endpoint with and without
the skip_cache flag to compare full-stack response times including parsing,
library search, Discogs lookups, and artwork fetching.

Each query exercises a different search path through the pipeline:
  A: Artist + Album    (artwork only, 1-5 API calls)
  B: Song lookup       (Discogs resolves album, 2-7 API calls)
  C: Track validation  (fallback + per-result validation, 12-22 API calls)
  D: Compilation       (cross-reference Discogs tracklists, 3-9 API calls)
  E: Artist only       (no song/album, 1-5 API calls)

Unlike benchmark_cache.py (which benchmarks search() directly against PG
cache vs API), this script measures the full request pipeline as seen by
end users.

Usage:
    venv/bin/python scripts/benchmark_requests.py --staging
    venv/bin/python scripts/benchmark_requests.py --local --cached-iterations 20
    venv/bin/python scripts/benchmark_requests.py --production --skip-warmup
"""

import argparse
import asyncio
import logging
import statistics
import sys
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

PROD_URL = "https://request-o-matic-production.up.railway.app/api/v1"
STAGING_URL = "https://request-o-matic-staging.up.railway.app/api/v1"
LOCAL_URL = "http://localhost:8000/api/v1"

# One representative query per search path, drawn from integration tests.
QUERIES: list[dict[str, str]] = [
    {
        "path": "A",
        "label": "Artist + Album",
        "message": "Meet Me in the City Junior Kimbrough",
        "api_calls": "1-5",
    },
    {
        "path": "B",
        "label": "Song lookup",
        "message": "Cult of Personality by Living Color",
        "api_calls": "2-7",
    },
    {
        "path": "C",
        "label": "Track validation",
        "message": "6 underground - sneaker pimps",
        "api_calls": "12-22",
    },
    {
        "path": "D",
        "label": "Compilation",
        "message": "Abele dance (85 remix) by Manu Dibango",
        "api_calls": "3-9",
    },
    {
        "path": "E",
        "label": "Artist only",
        "message": "echo and the bunnymen",
        "api_calls": "1-5",
    },
]


async def run_query(
    client: httpx.AsyncClient,
    base_url: str,
    message: str,
    skip_cache: bool,
) -> dict[str, Any]:
    """Send a single request and return timing and cache stats."""
    start = time.perf_counter()
    resp = await client.post(
        f"{base_url}/request",
        json={"message": message, "skip_slack": True, "skip_cache": skip_cache},
    )
    elapsed_ms = (time.perf_counter() - start) * 1000
    resp.raise_for_status()
    data = resp.json()
    stats = data.get("cache_stats", {})
    return {
        "elapsed_ms": elapsed_ms,
        "memory_hits": stats.get("memory_hits", 0),
        "pg_hits": stats.get("pg_hits", 0),
        "api_calls": stats.get("api_calls", 0),
        "pg_time_ms": stats.get("pg_time_ms", 0),
        "api_time_ms": stats.get("api_time_ms", 0),
        "results": len(data.get("library_results", [])),
    }


async def run_benchmark(
    base_url: str,
    cached_iterations: int = 10,
    skip_warmup: bool = False,
) -> None:
    """Run the full benchmark suite.

    Strategy:
    - Run each query uncached ONCE (to avoid burning API rate limits)
    - Run each query cached N times (free -- no API calls)
    - Report median and p95 for the cached side
    """
    async with httpx.AsyncClient(timeout=120.0) as client:
        # Warm up: populate caches with each query
        if not skip_warmup:
            print("  Warming up caches...")
            for q in QUERIES:
                try:
                    await run_query(client, base_url, q["message"], skip_cache=False)
                except httpx.HTTPStatusError as e:
                    print(
                        f"  WARNING: Warmup failed for path {q['path']}: {e.response.status_code}"
                    )
            print()

        # Header
        print(
            f"  {'Path':<6}{'Label':<22}{'Uncached':>10}"
            f"{'Cached (median)':>16}{'Cached (p95)':>14}"
            f"{'Speedup':>10}{'API calls':>11}"
        )
        print("  " + "-" * 87)

        all_cached_medians: list[float] = []
        all_uncached: list[float] = []

        for q in QUERIES:
            # Uncached: run once
            try:
                uncached = await run_query(client, base_url, q["message"], skip_cache=True)
            except httpx.HTTPStatusError as e:
                print(
                    f"  {q['path']:<6}{q['label']:<22}  ERROR (uncached): {e.response.status_code}"
                )
                continue

            # Cached: run N times
            cached_times: list[float] = []
            for _ in range(cached_iterations):
                try:
                    result = await run_query(client, base_url, q["message"], skip_cache=False)
                    cached_times.append(result["elapsed_ms"])
                except httpx.HTTPStatusError:
                    pass

            if not cached_times:
                print(f"  {q['path']:<6}{q['label']:<22}  ERROR (cached): all iterations failed")
                continue

            median_cached = statistics.median(cached_times)
            p95_cached = (
                sorted(cached_times)[int(len(cached_times) * 0.95)]
                if len(cached_times) >= 2
                else cached_times[0]
            )
            speedup = uncached["elapsed_ms"] / median_cached if median_cached > 0 else float("inf")

            all_cached_medians.append(median_cached)
            all_uncached.append(uncached["elapsed_ms"])

            print(
                f"  {q['path']:<6}{q['label']:<22}"
                f"{uncached['elapsed_ms']:>9.0f}ms"
                f"{median_cached:>13.0f}ms"
                f"{p95_cached:>13.0f}ms"
                f"{speedup:>9.1f}x"
                f"{uncached['api_calls']:>7} ({q['api_calls']})"
            )

        # Summary
        if all_cached_medians and all_uncached:
            avg_cached = statistics.mean(all_cached_medians)
            avg_uncached = statistics.mean(all_uncached)
            overall_speedup = avg_uncached / avg_cached if avg_cached > 0 else float("inf")
            print("  " + "-" * 87)
            print(f"  {'':>28}{'avg':>10}{'avg':>16}{'':>14}{'':>10}")
            print(
                f"  {'OVERALL':<28}"
                f"{avg_uncached:>9.0f}ms"
                f"{avg_cached:>13.0f}ms"
                f"{'':>14}"
                f"{overall_speedup:>9.1f}x"
            )

    print()
    print(f"  Cached iterations per query: {cached_iterations}")
    print("  Uncached iterations per query: 1 (to preserve API rate limits)")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark end-to-end request latency: cached vs skip_cache.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Search paths benchmarked:
  A  Artist + Album       artwork only               (1-5 API calls)
  B  Song lookup          Discogs resolves album      (2-7 API calls)
  C  Track validation     fallback + per-result check (12-22 API calls)
  D  Compilation          Discogs tracklist xref      (3-9 API calls)
  E  Artist only          no song/album               (1-5 API calls)

Examples:
  %(prog)s --staging
  %(prog)s --local --cached-iterations 50
  %(prog)s --production --skip-warmup
        """,
    )

    server_group = parser.add_mutually_exclusive_group()
    server_group.add_argument(
        "-l", "--local", action="store_true", help="Benchmark local server (default)"
    )
    server_group.add_argument(
        "-s", "--staging", action="store_true", help="Benchmark staging server"
    )
    server_group.add_argument(
        "-p", "--production", action="store_true", help="Benchmark production server"
    )
    parser.add_argument(
        "-n",
        "--cached-iterations",
        type=int,
        default=10,
        help="Number of cached iterations per query (default: 10)",
    )
    parser.add_argument(
        "--skip-warmup",
        action="store_true",
        help="Skip cache warmup (useful if caches are already populated)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")

    args = parser.parse_args()

    level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(level=level, format="%(asctime)s - %(levelname)s - %(message)s")

    if args.staging:
        base_url = STAGING_URL
    elif args.production:
        base_url = PROD_URL
    else:
        base_url = LOCAL_URL

    print()
    print("  Request Benchmark: cached vs skip_cache")
    print("  " + "=" * 42)
    print(f"  Server: {base_url}")
    print()

    try:
        asyncio.run(
            run_benchmark(
                base_url, cached_iterations=args.cached_iterations, skip_warmup=args.skip_warmup
            )
        )
    except KeyboardInterrupt:
        print("\n  Interrupted.")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Benchmark failed: {e}")
        if args.verbose:
            import traceback

            traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
