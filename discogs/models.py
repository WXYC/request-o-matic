"""Pydantic models for Discogs API responses."""
from typing import Optional

from pydantic import BaseModel


class TrackItem(BaseModel):
    """A single track on a release."""
    position: str
    title: str
    duration: Optional[str] = None


class TrackAlbumResponse(BaseModel):
    """Response for track-to-album lookup."""
    album: Optional[str] = None
    artist: Optional[str] = None
    release_id: Optional[int] = None
    release_url: Optional[str] = None
    cached: bool = False


class ReleaseInfo(BaseModel):
    """Information about a single release containing a track."""
    album: str
    artist: str
    release_id: int
    release_url: str
    is_compilation: bool = False


class TrackReleasesResponse(BaseModel):
    """Response for finding all releases containing a track."""
    track: str
    artist: Optional[str] = None
    releases: list[ReleaseInfo] = []
    total: int = 0
    cached: bool = False


class ReleaseMetadataResponse(BaseModel):
    """Full release metadata from Discogs."""
    release_id: int
    title: str
    artist: str
    year: Optional[int] = None
    label: Optional[str] = None
    genres: list[str] = []
    styles: list[str] = []
    tracklist: list[TrackItem] = []
    artwork_url: Optional[str] = None
    release_url: str
    cached: bool = False


class DiscogsSearchRequest(BaseModel):
    """Request for general Discogs search."""
    artist: Optional[str] = None
    album: Optional[str] = None
    track: Optional[str] = None


class DiscogsSearchResult(BaseModel):
    """A single result from Discogs search."""
    album: Optional[str] = None
    artist: Optional[str] = None
    release_id: int
    release_url: str
    artwork_url: Optional[str] = None
    confidence: float = 0.0


class DiscogsSearchResponse(BaseModel):
    """Response for general Discogs search."""
    results: list[DiscogsSearchResult] = []
    total: int = 0
    cached: bool = False
