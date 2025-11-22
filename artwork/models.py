from typing import Optional

from pydantic import BaseModel


class ArtworkRequest(BaseModel):
    """Request to find album artwork."""

    song: Optional[str] = None
    album: Optional[str] = None
    artist: Optional[str] = None


class ArtworkResponse(BaseModel):
    """Response containing artwork URL and metadata."""

    artwork_url: Optional[str] = None
    album: Optional[str] = None
    artist: Optional[str] = None
    source: Optional[str] = None
    confidence: float = 0.0


class SearchResult(BaseModel):
    """A single search result from a provider."""

    artwork_url: str
    album: str
    artist: str
    source: str
    confidence: float
