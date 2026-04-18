"""Shared Pydantic models for library and release metadata.

These DTOs are used to deserialize responses from the library-metadata-lookup
service. The full library and Discogs modules (search, caching, rate limiting)
now live exclusively in that service.
"""

from pydantic import BaseModel, computed_field


class LibraryItem(BaseModel):
    """A single item from the library catalog."""

    id: int
    title: str | None = None
    artist: str | None = None
    call_letters: str | None = None
    artist_call_number: int | None = None
    release_call_number: int | None = None
    genre: str | None = None
    format: str | None = None
    alternate_artist_name: str | None = None

    @property
    def call_number(self) -> str:
        """Full call number for shelf lookup: <Genre> <Format> <Letters> <ArtistNum>/<ReleaseNum>"""
        parts = []
        if self.genre:
            parts.append(self.genre)
        if self.format:
            parts.append(self.format)
        if self.call_letters:
            parts.append(self.call_letters)
        if self.artist_call_number is not None:
            parts.append(str(self.artist_call_number))
        if self.release_call_number is not None:
            parts[-1] = f"{parts[-1]}/{self.release_call_number}"
        return " ".join(parts)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def library_url(self) -> str:
        """URL to view this release in the WXYC library."""
        return f"http://www.wxyc.info/wxycdb/libraryRelease?id={self.id}"


class ReleaseMetadata(BaseModel):
    """Metadata for a release: Discogs info, artwork, and streaming links.

    Deserialized from the ``artwork`` field of library-metadata-lookup responses.
    """

    album: str | None = None
    artist: str | None = None
    release_id: int
    artwork_url: str | None = None
    confidence: float = 0.0
    release_url: str = ""
    spotify_url: str | None = None
    apple_music_url: str | None = None
    youtube_music_url: str | None = None
    bandcamp_url: str | None = None
    soundcloud_url: str | None = None

    @property
    def preview_url(self) -> str | None:
        """First available streaming URL in priority order."""
        for url in (
            self.bandcamp_url,
            self.spotify_url,
            self.apple_music_url,
            self.youtube_music_url,
            self.soundcloud_url,
        ):
            if url:
                return url
        return None
