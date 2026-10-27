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
                "bandcamp_url": "https://b.example/1",
                "spotify_url": "https://s.example/1",
                "apple_music_url": "https://a.example/1",
                "youtube_music_url": "https://y.example/1",
                "soundcloud_url": "https://sc.example/1",
            },
            "https://b.example/1",
        ),
        # Spotify beats Apple/YouTube/SoundCloud when no Bandcamp
        (
            {
                "spotify_url": "https://s.example/1",
                "apple_music_url": "https://a.example/1",
                "youtube_music_url": "https://y.example/1",
                "soundcloud_url": "https://sc.example/1",
            },
            "https://s.example/1",
        ),
        # Apple beats YouTube/SoundCloud when no Bandcamp/Spotify
        (
            {
                "apple_music_url": "https://a.example/1",
                "youtube_music_url": "https://y.example/1",
                "soundcloud_url": "https://sc.example/1",
            },
            "https://a.example/1",
        ),
        # YouTube beats SoundCloud
        (
            {
                "youtube_music_url": "https://y.example/1",
                "soundcloud_url": "https://sc.example/1",
            },
            "https://y.example/1",
        ),
    ],
)
def test_priority_order(kwargs, expected):
    metadata = make_release_metadata(**kwargs)
    assert preview_url(metadata) == expected


def test_returns_none_when_no_streaming_urls_set():
    metadata = make_release_metadata()
    assert preview_url(metadata) is None


def test_empty_string_url_falls_through_to_the_next_source():
    """The streaming fields are plain ``str`` (the #428 pin), so an empty string
    is accepted at decode time and the helper's truthiness guard skips it."""
    metadata = make_release_metadata(bandcamp_url="", spotify_url="https://s.example/1")
    assert preview_url(metadata) == "https://s.example/1"


def test_bare_domain_bandcamp_url_is_returned_unchanged():
    """Pin the absence of ``AnyUrl`` normalization on the most commonly posted link shape.

    With the five streaming fields held at ``str`` (#428 pin), a bare-domain URL is
    not given a trailing slash; the value is returned exactly as posted.
    """
    metadata = make_release_metadata(bandcamp_url="https://juanamolina.bandcamp.com")
    assert preview_url(metadata) == "https://juanamolina.bandcamp.com"


def test_url_carrying_a_path_is_returned_byte_for_byte():
    """Normalization only affects bare domains -- a URL with a path is untouched."""
    metadata = make_release_metadata(
        apple_music_url="https://music.apple.com/us/album/doga/123?i=4"
    )
    assert preview_url(metadata) == "https://music.apple.com/us/album/doga/123?i=4"


def test_returns_a_plain_str_not_a_url_object():
    """``services/slack.py`` interpolates the result into a Slack mrkdwn link."""
    metadata = make_release_metadata(spotify_url="https://open.spotify.com/album/abc")
    assert type(preview_url(metadata)) is str
