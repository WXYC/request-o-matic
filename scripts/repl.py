#!/usr/bin/env python3
"""Interactive REPL for testing the /request endpoint.

Usage:
    python scripts/repl.py
    python scripts/repl.py --local    # Use local server instead of production
    python scripts/repl.py --verbose  # Enable debug logging
"""

import argparse
import asyncio
import logging
import readline
import sys
from pathlib import Path
from typing import Any, cast

# Allow running from any directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from scripts._common import LOCAL_URL, PROD_URL, set_up_logging

# History file location
HISTORY_FILE = Path.home() / ".request_repl_history"


def configure_readline() -> None:
    """Configure readline for proper keybindings."""
    if "libedit" in (readline.__doc__ or ""):
        # macOS libedit - bind common editing keys
        readline.parse_and_bind("bind ^A ed-move-to-beg")
        readline.parse_and_bind("bind ^E ed-move-to-end")
        readline.parse_and_bind("bind ^U vi-kill-line-prev")
        readline.parse_and_bind("bind ^K ed-kill-line")
        readline.parse_and_bind("bind ^L ed-clear-screen")
        readline.parse_and_bind("bind ^W ed-delete-prev-word")
        # Arrow keys
        readline.parse_and_bind("bind ^[[A ed-search-prev-history")
        readline.parse_and_bind("bind ^[[B ed-search-next-history")
        readline.parse_and_bind("bind ^[[C ed-next-char")
        readline.parse_and_bind("bind ^[[D ed-prev-char")
    else:
        readline.parse_and_bind("set editing-mode emacs")


logger = logging.getLogger(__name__)


def print_result(data: dict) -> None:
    """Print the lookup result."""
    parsed = data.get("parsed", {})
    results = data.get("library_results", [])
    artwork = data.get("artwork")

    # Parsed info
    print(f"\n  {'Parsed':-^50}")
    print(f"  Is Request:  {parsed.get('is_request')}")
    print(f"  Type:        {parsed.get('message_type')}")
    if parsed.get("artist"):
        print(f"  Artist:      {parsed.get('artist')}")
    if parsed.get("song"):
        print(f"  Song:        {parsed.get('song')}")
    if parsed.get("album"):
        print(f"  Album:       {parsed.get('album')}")

    # Get artwork info - release_url only matches the artwork album
    artwork_url = artwork.get("release_url") if artwork else None
    artwork_album = (artwork.get("album") or "").lower() if artwork else ""

    # Library results
    print(f"\n  {'Library Results':-^50}")
    if not results:
        print("  No results found.")
    else:
        for i, item in enumerate(results, 1):
            title = item.get("title", "")
            artist = item.get("artist", "")
            print(f"  [{i}] {title}")
            print(f"      Artist:   {artist}")
            if item.get("genre"):
                print(f"      Genre:    {item.get('genre')}")
            call_letters = item.get("call_letters", "")
            artist_num = item.get("artist_call_number", "")
            release_num = item.get("release_call_number", "")
            if call_letters:
                print(f"      Location: {call_letters} {artist_num}/{release_num}")
            # Only show Discogs URL if we have a confirmed match from artwork
            if artwork_url and title.lower() == artwork_album:
                print(f"      Discogs:  {artwork_url}")
            print(f"      WXYC:     {item.get('library_url') or '(none)'}")

    # Artwork
    if artwork and artwork.get("artwork_url"):
        print(f"\n  {'Artwork':-^50}")
        print(f"  Image:      {artwork.get('artwork_url')}")
        if artwork.get("release_url"):
            print(f"  Discogs:    {artwork.get('release_url')}")
        print(f"  Confidence: {artwork.get('confidence', 0):.2f}")

    print()


async def lookup(client: httpx.AsyncClient, base_url: str, query: str) -> dict[str, Any] | None:
    """Execute a lookup query."""
    try:
        response = await client.post(
            f"{base_url}/request",
            json={"message": query, "skip_slack": True},
        )
        response.raise_for_status()
        return cast(dict[str, Any], response.json())
    except httpx.HTTPStatusError as e:
        print(f"  Error: {e.response.status_code} - {e.response.text}")
        return None
    except httpx.RequestError as e:
        print(f"  Connection error: {e}")
        return None


def load_history() -> None:
    """Load command history from file."""
    try:
        if HISTORY_FILE.exists():
            readline.read_history_file(HISTORY_FILE)
    except Exception:
        pass  # Ignore history load errors


def save_history() -> None:
    """Save command history to file."""
    try:
        readline.set_history_length(1000)
        readline.write_history_file(HISTORY_FILE)
    except Exception:
        pass  # Ignore history save errors


async def repl(base_url: str, verbose: bool) -> None:
    """Run the interactive REPL."""
    set_up_logging(verbose, default_level=logging.WARNING)
    configure_readline()
    load_history()

    print("=" * 60)
    print("  Request-O-Matic REPL")
    print("=" * 60)
    print(f"  Server: {base_url}")
    print("  Type a query and press Enter. Type :h for help.")
    print("=" * 60)

    current_url = base_url
    current_verbose = verbose

    async with httpx.AsyncClient(timeout=60.0) as client:
        while True:
            try:
                query = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nBye!")
                save_history()
                break

            if not query:
                continue

            # Handle commands
            if query.lower() in (":q", ":quit", ":exit"):
                print("Bye!")
                save_history()
                break
            elif query.lower() in (":h", ":help"):
                print("  Commands:")
                print("    :h, :help         - Show this help")
                print("    :q, :quit, :exit  - Exit the REPL")
                print("    :v, :verbose      - Toggle verbose mode")
                print("    :l, :local        - Switch to local server")
                print("    :p, :prod         - Switch to production server")
                print()
                print("  Type any other text to search for a song/artist/album.")
                continue
            elif query.lower() in (":v", ":verbose"):
                current_verbose = not current_verbose
                set_up_logging(current_verbose)
                print(f"  Verbose mode: {'on' if current_verbose else 'off'}")
                continue
            elif query.lower() in (":l", ":local"):
                current_url = LOCAL_URL
                print(f"  Switched to: {current_url}")
                continue
            elif query.lower() in (":p", ":prod"):
                current_url = PROD_URL
                print(f"  Switched to: {current_url}")
                continue
            elif query.startswith(":"):
                print(f"  Unknown command: {query}. Type :h for help.")
                continue

            # Execute lookup
            data = await lookup(client, current_url, query)
            if data:
                print_result(data)


def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Interactive REPL for testing the /request endpoint.",
    )
    parser.add_argument(
        "-l",
        "--local",
        action="store_true",
        help="Use local server (localhost:8000) instead of production",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose/debug logging",
    )

    args = parser.parse_args()
    base_url = LOCAL_URL if args.local else PROD_URL

    try:
        asyncio.run(repl(base_url, args.verbose))
    except KeyboardInterrupt:
        print("\nBye!")
        sys.exit(0)


if __name__ == "__main__":
    main()
