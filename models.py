"""Shared DTOs for library and release metadata.

Types are generated from ``wxyc-shared/api.yaml`` (see ``scripts/generate_api_models.sh``)
and re-exported here under their historical request-o-matic names. The free
``preview_url`` helper supplies the streaming-priority logic that used to live
on ``ReleaseMetadata`` as a ``@property`` — the shared schema is data-only.
"""

from generated.api_models import DiscogsMatchResult, LibraryCatalogItem

LibraryItem = LibraryCatalogItem
ReleaseMetadata = DiscogsMatchResult


def preview_url(metadata: DiscogsMatchResult) -> str | None:
    """First available streaming URL, prioritizing Bandcamp.

    The shared contract types the five streaming fields as ``AnyUrl``, so the
    parsed values are stringified here at the boundary: ``services/slack.py``
    interpolates the result straight into a Slack mrkdwn link and needs a
    ``str``. Pydantic normalizes a bare-domain URL by appending a trailing
    slash, so a Bandcamp artist link comes back as
    ``https://artist.bandcamp.com/`` rather than the exact upstream string.
    That is deliberate (#286): the link resolves identically, and preserving
    the byte-for-byte original would mean carrying the raw payload value
    alongside the parsed one for no functional gain.
    """
    for url in (
        metadata.bandcamp_url,
        metadata.spotify_url,
        metadata.apple_music_url,
        metadata.youtube_music_url,
        metadata.soundcloud_url,
    ):
        if url:
            return str(url)
    return None


__all__ = ["LibraryItem", "ReleaseMetadata", "preview_url"]
