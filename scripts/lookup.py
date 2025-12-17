#!/usr/bin/env python3
"""CLI script to test the /request endpoint without posting to Slack.

Usage:
    python scripts/lookup.py "play bohemian rhapsody by queen"
    python scripts/lookup.py --verbose "the beatles abbey road"
"""
import argparse
import asyncio
import logging
import sys
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://request-o-matic-production.up.railway.app/api/v1"


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


def print_library_results(results: list[dict], artwork: Optional[dict]) -> None:
    """Print library search results."""
    print_section("Library Results")

    if not results:
        print("  No results found in library.")
        return

    for i, item in enumerate(results, 1):
        print(f"  [{i}] {item.get('title')}")
        print(f"      Artist:   {item.get('artist')}")
        print(f"      Genre:    {item.get('genre') or '(none)'}")
        print(f"      Format:   {item.get('format') or '(none)'}")
        print(f"      Location: {item.get('call_number') or '(none)'}")
        print(f"      URL:      {item.get('library_url') or '(none)'}")
        print()

    if artwork and artwork.get("url"):
        print_section("Artwork")
        print(f"  URL:        {artwork.get('url')}")
        print(f"  Source:     {artwork.get('source')}")
        print(f"  Confidence: {artwork.get('confidence', 0):.2f}")


async def run_lookup(query: str, verbose: bool = False) -> dict:
    """Call the /request endpoint with skip_slack=true."""
    set_up_logging(verbose)
    logger.info(f"Processing query: {query}")
    logger.info(f"Using API: {BASE_URL}")

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            logger.info("Calling /request endpoint...")
            response = await client.post(
                f"{BASE_URL}/request",
                json={"message": query, "skip_slack": True},
            )
            response.raise_for_status()
            data = response.json()

            # Display results
            print_parsed_request(data.get("parsed", {}))
            print_library_results(
                data.get("library_results", []),
                data.get("artwork"),
            )

            return data

        except httpx.HTTPStatusError as e:
            logger.error(f"Request failed: {e.response.status_code}")
            logger.error(f"Response: {e.response.text}")
            raise SystemExit(1)


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
