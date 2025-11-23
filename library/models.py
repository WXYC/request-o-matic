from typing import Optional

from pydantic import BaseModel, computed_field


class LibrarySearchRequest(BaseModel):
    """Request to search the library catalog."""

    query: Optional[str] = None
    artist: Optional[str] = None
    title: Optional[str] = None
    limit: int = 10


class LibraryItem(BaseModel):
    """A single item from the library catalog."""

    id: int
    title: Optional[str]
    artist: Optional[str]
    call_letters: Optional[str]
    artist_call_number: Optional[int]
    release_call_number: Optional[int]
    genre: Optional[str]
    format: Optional[str]

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

    @computed_field
    @property
    def library_url(self) -> str:
        """URL to view this release in the WXYC library."""
        return f"http://www.wxyc.info/wxycdb/libraryRelease?id={self.id}"


class LibrarySearchResponse(BaseModel):
    """Response containing library search results."""

    results: list[LibraryItem]
    total: int
    query: Optional[str] = None
