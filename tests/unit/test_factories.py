"""Unit tests for the test factories.

The factories build ``LibraryCatalogItem`` and ``DiscogsMatchResult`` with
fields the shared schema makes required (``call_number``, ``library_url``,
``release_url``). The call-number computation has several conditional branches
that no integration tests exercise individually, so they're pinned here.
"""

import pytest

from tests.factories import _compute_call_number, make_library_item, make_release_metadata


class TestComputeCallNumber:
    """Branch coverage for ``_compute_call_number``."""

    def test_all_fields_present(self):
        assert _compute_call_number("Rock", "CD", "S", 1, 1) == "Rock CD S 1/1"

    def test_no_release_call_number(self):
        assert _compute_call_number("Rock", "CD", "S", 1, None) == "Rock CD S 1"

    def test_no_genre(self):
        assert _compute_call_number(None, "CD", "S", 1, 1) == "CD S 1/1"

    def test_no_format(self):
        assert _compute_call_number("Rock", None, "S", 1, 1) == "Rock S 1/1"

    def test_no_call_letters(self):
        assert _compute_call_number("Rock", "CD", None, 1, 1) == "Rock CD 1/1"

    def test_only_artist_call_number(self):
        assert _compute_call_number(None, None, None, 1, None) == "1"

    def test_all_none_returns_empty_string(self):
        assert _compute_call_number(None, None, None, None, None) == ""

    def test_release_call_number_appends_to_last_part_when_artist_call_number_missing(self):
        """When artist_call_number is None but release_call_number is set, the suffix
        attaches to whatever the last part was — typically call_letters."""
        assert _compute_call_number("Rock", "CD", "S", None, 2) == "Rock CD S/2"

    def test_release_call_number_alone_is_dropped_when_no_other_parts(self):
        """release_call_number requires a non-empty parts list; otherwise it has nothing
        to suffix and is silently dropped."""
        assert _compute_call_number(None, None, None, None, 99) == ""


class TestMakeLibraryItem:
    """Defaults and override behavior for the LibraryCatalogItem factory."""

    def test_defaults_match_wxyc_canonical_artist(self):
        item = make_library_item()
        assert item.id == 1
        assert item.artist == "Stereolab"
        assert item.title == "Aluminum Tunes"
        assert item.genre == "Rock"
        assert item.format == "CD"

    def test_default_call_number_is_computed_from_other_fields(self):
        assert make_library_item().call_number == "Rock CD S 1/1"

    def test_default_library_url_includes_id(self):
        item = make_library_item(id=42)
        assert item.library_url == "https://dj.wxyc.org/dashboard/album/legacy/42"

    def test_call_number_recomputed_when_components_change(self):
        item = make_library_item(genre=None, format=None, release_call_number=None)
        assert item.call_number == "S 1"

    def test_explicit_call_number_overrides_computed(self):
        item = make_library_item(call_number="Custom 99")
        assert item.call_number == "Custom 99"

    def test_explicit_library_url_overrides_default(self):
        item = make_library_item(library_url="https://example.com/custom")
        assert item.library_url == "https://example.com/custom"


class TestMakeReleaseMetadata:
    """Defaults and overrides for the DiscogsMatchResult factory."""

    def test_default_release_url_uses_release_id(self):
        metadata = make_release_metadata(release_id=12345)
        assert metadata.release_url == "https://www.discogs.com/release/12345"

    def test_explicit_release_url_overrides_default(self):
        metadata = make_release_metadata(release_url="https://example.com/r/1")
        assert metadata.release_url == "https://example.com/r/1"

    def test_streaming_urls_default_to_none(self):
        metadata = make_release_metadata()
        assert metadata.spotify_url is None
        assert metadata.bandcamp_url is None
        assert metadata.apple_music_url is None
        assert metadata.youtube_music_url is None
        assert metadata.soundcloud_url is None

    @pytest.mark.parametrize(
        "field",
        ["spotify_url", "apple_music_url", "youtube_music_url", "bandcamp_url", "soundcloud_url"],
    )
    def test_streaming_url_pass_through(self, field: str) -> None:
        metadata = make_release_metadata(**{field: "https://example.com/s"})  # type: ignore[arg-type]
        assert getattr(metadata, field) == "https://example.com/s"
