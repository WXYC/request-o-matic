"""Contract parity tests between inline and delegated lookup pipelines.

These tests verify that both implementations return the same results for
identical inputs. They require:
- library.db on disk (for inline pipeline)
- DISCOGS_TOKEN env var (for inline pipeline)
- library-metadata-lookup service running (for HTTP pipeline)

Run with: pytest tests/contract/ -v -m contract
"""

import pytest


@pytest.mark.contract
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "artist, song, album, min_results",
    [
        pytest.param("Queen", None, "The Game", 1, id="direct_match"),
        pytest.param("Echo and the Bunnymen", None, None, 1, id="artist_only"),
        pytest.param("Junior Kimbrough", "Meet Me in the City", None, 1, id="album_resolution"),
        pytest.param("ZZZNONEXISTENT", None, None, 0, id="nonexistent_artist"),
    ],
)
async def test_parity_basic(
    inline_lookup,
    http_lookup,
    artist,
    song,
    album,
    min_results,
):
    """Both pipelines agree on basic search results."""
    inline = await inline_lookup(artist=artist, song=song, album=album)
    http = await http_lookup(artist=artist, song=song, album=album)

    # Both should return at least min_results
    assert (
        len(inline["item_ids"]) >= min_results
    ), f"Inline: expected >= {min_results} results, got {len(inline['item_ids'])}"
    assert (
        len(http["item_ids"]) >= min_results
    ), f"HTTP: expected >= {min_results} results, got {len(http['item_ids'])}"

    # Item IDs should match
    assert (
        inline["item_ids"] == http["item_ids"]
    ), f"Item ID mismatch:\n  Inline: {inline['item_ids']}\n  HTTP: {http['item_ids']}"

    # Search metadata should match
    assert (
        inline["search_type"] == http["search_type"]
    ), f"search_type mismatch: inline={inline['search_type']}, http={http['search_type']}"
    assert (
        inline["song_not_found"] == http["song_not_found"]
    ), f"song_not_found mismatch: inline={inline['song_not_found']}, http={http['song_not_found']}"
    assert inline["found_on_compilation"] == http["found_on_compilation"], (
        f"found_on_compilation mismatch: inline={inline['found_on_compilation']}, "
        f"http={http['found_on_compilation']}"
    )


@pytest.mark.contract
@pytest.mark.asyncio
async def test_parity_artist_correction(inline_lookup, http_lookup):
    """Both pipelines correct artist spelling the same way."""
    inline = await inline_lookup(artist="Living Color")
    http = await http_lookup(artist="Living Color")

    assert len(inline["item_ids"]) >= 1
    assert len(http["item_ids"]) >= 1
    assert inline["item_ids"] == http["item_ids"]
    assert inline["corrected_artist"] == http["corrected_artist"]
    assert inline["corrected_artist"] == "Living Colour"


@pytest.mark.contract
@pytest.mark.asyncio
async def test_parity_track_validation(inline_lookup, http_lookup):
    """Both pipelines filter results by track validation the same way."""
    inline = await inline_lookup(artist="Lush", song="Thoughtforms")
    http = await http_lookup(artist="Lush", song="Thoughtforms")

    assert inline["item_ids"] == http["item_ids"]
    assert inline["search_type"] == http["search_type"]
    assert inline["song_not_found"] == http["song_not_found"]


@pytest.mark.contract
@pytest.mark.asyncio
async def test_parity_prefix_matching(inline_lookup, http_lookup):
    """Both pipelines apply prefix matching to exclude false positives."""
    inline = await inline_lookup(artist="Toy")
    http = await http_lookup(artist="Toy")

    assert inline["item_ids"] == http["item_ids"]

    # Neither should include "Chew Toy" results
    # (We can't check artist names directly since we only have IDs,
    # but matching IDs confirms both apply the same filtering)


@pytest.mark.contract
@pytest.mark.asyncio
async def test_parity_compilation_exclusion(inline_lookup, http_lookup):
    """Both pipelines exclude unrelated compilations the same way."""
    inline = await inline_lookup(artist="Sugar Plant", song="Simple")
    http = await http_lookup(artist="Sugar Plant", song="Simple")

    assert inline["item_ids"] == http["item_ids"]
    assert inline["found_on_compilation"] == http["found_on_compilation"]


@pytest.mark.contract
@pytest.mark.asyncio
async def test_parity_ambiguous_format(inline_lookup, http_lookup):
    """Both pipelines handle ambiguous 'X - Y' format the same way."""
    raw = "Stereolab - Dots and Loops"
    inline = await inline_lookup(artist="Stereolab", album="Dots and Loops", raw_message=raw)
    http = await http_lookup(artist="Stereolab", album="Dots and Loops", raw_message=raw)

    assert len(inline["item_ids"]) >= 1
    assert len(http["item_ids"]) >= 1
    assert inline["item_ids"] == http["item_ids"]
