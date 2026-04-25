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
    """First available streaming URL, prioritizing Bandcamp."""
    for url in (
        metadata.bandcamp_url,
        metadata.spotify_url,
        metadata.apple_music_url,
        metadata.youtube_music_url,
        metadata.soundcloud_url,
    ):
        if url:
            return url
    return None


__all__ = ["LibraryItem", "ReleaseMetadata", "preview_url"]
