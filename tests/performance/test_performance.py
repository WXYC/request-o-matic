"""
Performance tests to measure query response times.

Run with: pytest tests/performance/ -v -m integration --tb=short -s

Set TEST_ENV to control which server to test against:
    TEST_ENV=local pytest ...      # localhost:8000 (default)
    TEST_ENV=staging pytest ...    # staging server on Railway
    TEST_ENV=production pytest ... # production server on Railway

These tests measure the impact of various search strategies:
1. Simple queries (artist already known)
2. Artist discovery via consensus voting
3. Compilation search
4. Ambiguous query handling
"""

import os
import statistics

# Import test environment utilities
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from dotenv import load_dotenv

from .baseline import (
    BaselineManager,
    check_performance,
    detect_environment,
)

sys.path.insert(0, str(Path(__file__).parent.parent))
from conftest import TestEnvironment, get_test_environment

load_dotenv()

pytestmark = pytest.mark.integration

GROQ_API_KEY = os.getenv("GROQ_API_KEY")

skip_if_no_groq = pytest.mark.skipif(
    not GROQ_API_KEY, reason="GROQ_API_KEY not set - skipping performance tests"
)

# Skip performance tests when running locally - they require too many API calls
# and timeout. Performance tests should only run on staging/production.
skip_if_local = pytest.mark.skipif(
    get_test_environment() == TestEnvironment.LOCAL,
    reason="Performance tests skipped for local environment - run with TEST_ENV=staging or TEST_ENV=production",
)


@dataclass
class TimingResult:
    """Result of a timed query."""

    query: str
    total_ms: float
    parsing_ms: float | None = None
    search_ms: float | None = None
    artwork_ms: float | None = None


class PerformanceMetrics:
    """Collect and report performance metrics."""

    def __init__(self):
        self.results: list[TimingResult] = []

    def add(self, result: TimingResult):
        self.results.append(result)

    def summary(self) -> dict:
        if not self.results:
            return {}

        times = [r.total_ms for r in self.results]
        return {
            "count": len(times),
            "min_ms": min(times),
            "max_ms": max(times),
            "mean_ms": statistics.mean(times),
            "median_ms": statistics.median(times),
            "stdev_ms": statistics.stdev(times) if len(times) > 1 else 0,
        }

    def print_report(self, title: str):
        print(f"\n{'=' * 60}")
        print(f"  {title}")
        print(f"{'=' * 60}")

        for result in self.results:
            print(f"  {result.query:<40} {result.total_ms:>8.1f}ms")

        summary = self.summary()
        if summary:
            print(f"{'-' * 60}")
            print(f"  {'Min:':<40} {summary['min_ms']:>8.1f}ms")
            print(f"  {'Max:':<40} {summary['max_ms']:>8.1f}ms")
            print(f"  {'Mean:':<40} {summary['mean_ms']:>8.1f}ms")
            print(f"  {'Median:':<40} {summary['median_ms']:>8.1f}ms")
            if summary["stdev_ms"] > 0:
                print(f"  {'Std Dev:':<40} {summary['stdev_ms']:>8.1f}ms")
        print()


@pytest.fixture
def environment(base_url):
    """Detect the test environment from base_url for baseline management."""
    return detect_environment(base_url)


@pytest.fixture(scope="session")
def baseline_manager():
    """Session-scoped baseline manager."""
    return BaselineManager()


@skip_if_local
class TestQueryPerformance:
    """Performance tests for different query types."""

    @pytest.mark.asyncio
    @skip_if_no_groq
    async def test_simple_queries_with_artist(self, base_url, environment, baseline_manager):
        """
        Measure response time for simple queries where artist is already known.

        These queries should be fast because:
        - No artist discovery needed
        - Direct library search with artist filter
        """
        queries = [
            "Bohemian Rhapsody by Queen",
            "Smells Like Teen Spirit by Nirvana",
            "Like a Rolling Stone by Bob Dylan",
            "Stairway to Heaven by Led Zeppelin",
            "Hotel California by Eagles",
        ]

        metrics = PerformanceMetrics()

        async with httpx.AsyncClient(timeout=30.0) as client:
            for query in queries:
                start = time.perf_counter()
                response = await client.post(
                    f"{base_url}/request",
                    json={"message": query, "skip_slack": True},
                )
                elapsed_ms = (time.perf_counter() - start) * 1000

                response.raise_for_status()
                metrics.add(TimingResult(query=query, total_ms=elapsed_ms))

        metrics.print_report("Simple Queries (Artist Known)")

        # Compare against baseline
        check_performance(
            metrics.summary(),
            test_name="test_simple_queries_with_artist",
            environment=environment,
            baseline_manager=baseline_manager,
        )

    @pytest.mark.asyncio
    @skip_if_no_groq
    async def test_artist_discovery_queries(self, base_url, environment, baseline_manager):
        """
        Measure response time for queries requiring artist discovery.

        These queries are slower because:
        - Extra Discogs API call for artist discovery
        - Consensus voting algorithm
        """
        queries = [
            "Siberian Nights",  # Should discover Twilight 22
            "Electric Kingdom",  # Should discover Twilight 22
            "Planet Rock",  # Should discover Afrika Bambaataa
            "The Message",  # Might have consensus issues
            "White Lines",  # Should discover Grandmaster Flash
        ]

        metrics = PerformanceMetrics()

        async with httpx.AsyncClient(timeout=30.0) as client:
            for query in queries:
                start = time.perf_counter()
                response = await client.post(
                    f"{base_url}/request",
                    json={"message": query, "skip_slack": True},
                )
                elapsed_ms = (time.perf_counter() - start) * 1000

                response.raise_for_status()
                data = response.json()

                # Note if artist was discovered
                artist = data.get("parsed", {}).get("artist")
                query_note = f"{query} -> {artist or 'no artist'}"
                metrics.add(TimingResult(query=query_note, total_ms=elapsed_ms))

        metrics.print_report("Artist Discovery Queries")

        # Compare against baseline
        check_performance(
            metrics.summary(),
            test_name="test_artist_discovery_queries",
            environment=environment,
            baseline_manager=baseline_manager,
        )

    @pytest.mark.asyncio
    @skip_if_no_groq
    async def test_compilation_search_queries(self, base_url, environment, baseline_manager):
        """
        Measure response time for queries that trigger compilation search.

        These are the slowest because:
        - Artist discovery (if needed)
        - Album lookup
        - Library search with fallback
        - Compilation search with multiple Discogs API calls
        """
        queries = [
            # Song + artist format - should trigger compilation search if not in library
            "Siberian Nights by Twilight 22",
            "Electric Kingdom by Twilight 22",
            "Planet Rock by Afrika Bambaataa",
            "Trans-Europe Express by Kraftwerk",
            "Numbers by Kraftwerk",
        ]

        metrics = PerformanceMetrics()

        async with httpx.AsyncClient(timeout=30.0) as client:
            for query in queries:
                start = time.perf_counter()
                response = await client.post(
                    f"{base_url}/request",
                    json={"message": query, "skip_slack": True},
                )
                elapsed_ms = (time.perf_counter() - start) * 1000

                response.raise_for_status()
                data = response.json()

                # Note result count
                results = len(data.get("library_results", []))
                query_note = f"{query[:30]:<30} ({results} results)"
                metrics.add(TimingResult(query=query_note, total_ms=elapsed_ms))

        metrics.print_report("Compilation Search Queries")

        # Compare against baseline
        check_performance(
            metrics.summary(),
            test_name="test_compilation_search_queries",
            environment=environment,
            baseline_manager=baseline_manager,
        )

    @pytest.mark.asyncio
    @skip_if_no_groq
    async def test_ambiguous_queries(self, base_url, environment, baseline_manager):
        """
        Measure response time for ambiguous queries.

        These test the "no consensus" path where artist discovery fails.
        """
        queries = [
            "Laid Back",  # No consensus - returns empty
            "Toy",  # Ambiguous
            "Young Gov",  # Ambiguous
            "Love",  # Very ambiguous
            "Dreams",  # Very ambiguous
        ]

        metrics = PerformanceMetrics()

        async with httpx.AsyncClient(timeout=30.0) as client:
            for query in queries:
                start = time.perf_counter()
                response = await client.post(
                    f"{base_url}/request",
                    json={"message": query, "skip_slack": True},
                )
                elapsed_ms = (time.perf_counter() - start) * 1000

                response.raise_for_status()
                data = response.json()

                artist = data.get("parsed", {}).get("artist")
                results = len(data.get("library_results", []))
                query_note = f"{query} -> {artist or 'none'} ({results})"
                metrics.add(TimingResult(query=query_note, total_ms=elapsed_ms))

        metrics.print_report("Ambiguous Queries (No Consensus)")

        # Compare against baseline
        check_performance(
            metrics.summary(),
            test_name="test_ambiguous_queries",
            environment=environment,
            baseline_manager=baseline_manager,
        )

    @pytest.mark.asyncio
    @skip_if_no_groq
    async def test_comparison_with_vs_without_artist(self, base_url):
        """
        Compare response times: same song with vs without artist.

        This directly measures the overhead of artist discovery.
        """
        test_cases = [
            ("Siberian Nights", "Siberian Nights by Twilight 22"),
            ("Planet Rock", "Planet Rock by Afrika Bambaataa"),
            ("The Message", "The Message by Grandmaster Flash"),
        ]

        print(f"\n{'=' * 70}")
        print("  Artist Discovery Overhead Comparison")
        print(f"{'=' * 70}")
        print(f"  {'Query':<35} {'Without Artist':>12} {'With Artist':>12} {'Diff':>10}")
        print(f"{'-' * 70}")

        async with httpx.AsyncClient(timeout=30.0) as client:
            for without_artist, with_artist in test_cases:
                # Without artist (needs discovery)
                start = time.perf_counter()
                await client.post(
                    f"{base_url}/request",
                    json={"message": without_artist, "skip_slack": True},
                )
                time_without = (time.perf_counter() - start) * 1000

                # With artist (no discovery needed)
                start = time.perf_counter()
                await client.post(
                    f"{base_url}/request",
                    json={"message": with_artist, "skip_slack": True},
                )
                time_with = (time.perf_counter() - start) * 1000

                diff = time_without - time_with
                print(
                    f"  {without_artist:<35} {time_without:>10.1f}ms {time_with:>10.1f}ms {diff:>+10.1f}ms"
                )

        print()

    @pytest.mark.asyncio
    @skip_if_no_groq
    async def test_repeated_queries_caching(self, base_url):
        """
        Test if repeated queries benefit from any caching.

        Run the same query multiple times to see if performance improves.
        """
        query = "Siberian Nights by Twilight 22"
        iterations = 5

        metrics = PerformanceMetrics()

        async with httpx.AsyncClient(timeout=30.0) as client:
            for i in range(iterations):
                start = time.perf_counter()
                response = await client.post(
                    f"{base_url}/request",
                    json={"message": query, "skip_slack": True},
                )
                elapsed_ms = (time.perf_counter() - start) * 1000

                response.raise_for_status()
                metrics.add(TimingResult(query=f"Iteration {i + 1}", total_ms=elapsed_ms))

        metrics.print_report(f"Repeated Query: {query}")

        # Check if later iterations are faster (caching effect)
        times = [r.total_ms for r in metrics.results]
        first_time = times[0]
        avg_later = statistics.mean(times[1:])

        print(f"  First query: {first_time:.1f}ms")
        print(f"  Avg of later queries: {avg_later:.1f}ms")
        if avg_later < first_time:
            improvement = ((first_time - avg_later) / first_time) * 100
            print(f"  Caching improvement: {improvement:.1f}%")
        print()


@skip_if_local
class TestApiCallCounts:
    """Test to understand how many API calls different queries make."""

    @pytest.mark.asyncio
    @skip_if_no_groq
    async def test_api_call_breakdown(self, base_url):
        """
        Analyze the theoretical API call counts for different query types.

        This doesn't actually count calls but documents expected behavior.
        """
        print(f"\n{'=' * 70}")
        print("  Expected API Call Breakdown by Query Type")
        print(f"{'=' * 70}")

        scenarios: list[dict[str, Any]] = [
            {
                "name": "Simple query with artist",
                "example": "Bohemian Rhapsody by Queen",
                "calls": {
                    "Groq (parsing)": 1,
                    "Discogs (artist discovery)": 0,
                    "Discogs (album lookup)": 1,
                    "Discogs (compilation search)": 0,
                    "Discogs (artwork)": "1-5",
                },
            },
            {
                "name": "Song only - artist discovered",
                "example": "Siberian Nights",
                "calls": {
                    "Groq (parsing)": 1,
                    "Discogs (artist discovery)": 1,
                    "Discogs (album lookup)": 1,
                    "Discogs (compilation search)": "2 (with + without artist)",
                    "Discogs (artwork)": "1-5",
                },
            },
            {
                "name": "Song only - no consensus",
                "example": "Laid Back",
                "calls": {
                    "Groq (parsing)": 1,
                    "Discogs (artist discovery)": 1,
                    "Discogs (album lookup)": 1,
                    "Discogs (compilation search)": 0,
                    "Discogs (artwork)": 0,
                },
            },
            {
                "name": "Compilation search triggered",
                "example": "Siberian Nights by Twilight 22",
                "calls": {
                    "Groq (parsing)": 1,
                    "Discogs (artist discovery)": 0,
                    "Discogs (album lookup)": 1,
                    "Discogs (compilation search)": "2 + N library searches",
                    "Discogs (artwork)": "1-5 per result",
                },
            },
        ]

        for scenario in scenarios:
            print(f"\n  {scenario['name']}")
            print(f'  Example: "{scenario["example"]}"')
            print(f"  {'-' * 50}")
            for api, count in scenario["calls"].items():
                print(f"    {api:<35} {count}")

        print()


@skip_if_local
class TestPerformanceSummary:
    """Generate a summary report of all performance characteristics."""

    @pytest.mark.asyncio
    @skip_if_no_groq
    async def test_full_performance_summary(self, base_url):
        """
        Run a comprehensive performance test and generate summary.
        """
        categories = {
            "With Artist (baseline)": [
                "Bohemian Rhapsody by Queen",
                "Smells Like Teen Spirit by Nirvana",
                "Stairway to Heaven by Led Zeppelin",
            ],
            "Artist Discovery (consensus found)": [
                "Siberian Nights",
                "Electric Kingdom",
            ],
            "Artist Discovery (no consensus)": [
                "Laid Back",
                "Toy",
            ],
            "Compilation Search": [
                "Siberian Nights by Twilight 22",
                "Holland, 1945 Neutral Milk Hotel",
            ],
        }

        print(f"\n{'=' * 70}")
        print("  PERFORMANCE SUMMARY REPORT")
        print(f"{'=' * 70}")

        all_metrics = {}

        async with httpx.AsyncClient(timeout=30.0) as client:
            for category, queries in categories.items():
                metrics = PerformanceMetrics()

                for query in queries:
                    start = time.perf_counter()
                    response = await client.post(
                        f"{base_url}/request",
                        json={"message": query, "skip_slack": True},
                    )
                    elapsed_ms = (time.perf_counter() - start) * 1000
                    response.raise_for_status()
                    metrics.add(TimingResult(query=query, total_ms=elapsed_ms))

                all_metrics[category] = metrics

        # Print summary table
        print(f"\n  {'Category':<40} {'Mean':>10} {'Min':>10} {'Max':>10}")
        print(f"  {'-' * 70}")

        for category, metrics in all_metrics.items():
            summary = metrics.summary()
            print(
                f"  {category:<40} {summary['mean_ms']:>8.1f}ms {summary['min_ms']:>8.1f}ms {summary['max_ms']:>8.1f}ms"
            )

        print(f"\n  {'-' * 70}")
        print("  Performance Impact Analysis:")

        baseline = all_metrics["With Artist (baseline)"].summary()["mean_ms"]

        for category, metrics in all_metrics.items():
            if category != "With Artist (baseline)":
                mean = metrics.summary()["mean_ms"]
                overhead = mean - baseline
                pct = (overhead / baseline) * 100 if baseline > 0 else 0
                print(f"    {category}: +{overhead:.0f}ms ({pct:+.0f}% vs baseline)")

        print()
