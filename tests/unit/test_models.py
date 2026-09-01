"""Unit tests for the ``preview_url`` helper.

The helper supplies the streaming-priority logic that used to live on
``ReleaseMetadata`` as a ``@property``. The shared ``DiscogsMatchResult``
schema is data-only, so this behavior lives in ``models.py`` now.
"""

import pytest
from pydantic import ValidationError

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


def test_empty_string_url_is_rejected_by_the_contract():
    """An empty string can no longer reach the helper at all.

    ``preview_url`` used to fall through empty strings to the next source. Now
    that the contract types the streaming fields as ``AnyUrl``, an empty string
    fails validation upstream, so the fall-through can only be reached by
    ``None``. The helper keeps its truthiness guard for exactly that case.
    """
    with pytest.raises(ValidationError):
        make_release_metadata(bandcamp_url="")


def test_bare_domain_bandcamp_url_gains_a_trailing_slash():
    """Pin the ``AnyUrl`` normalization on the most commonly posted link shape.

    The shared contract types the five streaming fields as ``AnyUrl``, and
    pydantic normalizes a bare-domain URL by appending a trailing slash.
    Bandcamp artist URLs are exactly that shape, and Bandcamp is the highest
    priority in ``preview_url``, so this is what the request channel sees. The
    link resolves identically -- but the behavior is now a property of this
    code, and should fail loudly rather than change silently again.
    """
    metadata = make_release_metadata(bandcamp_url="https://juanamolina.bandcamp.com")
    assert preview_url(metadata) == "https://juanamolina.bandcamp.com/"


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
