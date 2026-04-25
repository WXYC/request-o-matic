"""Unit tests for the ``preview_url`` helper.

The helper supplies the streaming-priority logic that used to live on
``ReleaseMetadata`` as a ``@property``. The shared ``DiscogsMatchResult``
schema is data-only, so this behavior lives in ``models.py`` now.
"""

import pytest

from models import preview_url
from tests.factories import make_release_metadata


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        # Single source set
        ({"bandcamp_url": "https://b.example/1"}, "https://b.example/1"),
        ({"spotify_url": "https://s.example/1"}, "https://s.example/1"),
        ({"apple_music_url": "https://a.example/1"}, "https://a.example/1"),
        ({"youtube_music_url": "https://y.example/1"}, "https://y.example/1"),
        ({"soundcloud_url": "https://sc.example/1"}, "https://sc.example/1"),
    ],
)
def test_returns_the_only_set_url(kwargs, expected):
    metadata = make_release_metadata(**kwargs)
    assert preview_url(metadata) == expected


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        # Bandcamp wins over everything
        (
            {
                "bandcamp_url": "https://b.example",
                "spotify_url": "https://s.example",
                "apple_music_url": "https://a.example",
                "youtube_music_url": "https://y.example",
                "soundcloud_url": "https://sc.example",
            },
            "https://b.example",
        ),
        # Spotify beats Apple/YouTube/SoundCloud when no Bandcamp
        (
            {
                "spotify_url": "https://s.example",
                "apple_music_url": "https://a.example",
                "youtube_music_url": "https://y.example",
                "soundcloud_url": "https://sc.example",
            },
            "https://s.example",
        ),
        # Apple beats YouTube/SoundCloud when no Bandcamp/Spotify
        (
            {
                "apple_music_url": "https://a.example",
                "youtube_music_url": "https://y.example",
                "soundcloud_url": "https://sc.example",
            },
            "https://a.example",
        ),
        # YouTube beats SoundCloud
        (
            {
                "youtube_music_url": "https://y.example",
                "soundcloud_url": "https://sc.example",
            },
            "https://y.example",
        ),
    ],
)
def test_priority_order(kwargs, expected):
    metadata = make_release_metadata(**kwargs)
    assert preview_url(metadata) == expected


def test_returns_none_when_no_streaming_urls_set():
    metadata = make_release_metadata()
    assert preview_url(metadata) is None


def test_skips_empty_string_urls():
    """Empty strings are falsy, so the helper should fall through to the next source."""
    metadata = make_release_metadata(bandcamp_url="", spotify_url="https://s.example")
    assert preview_url(metadata) == "https://s.example"
