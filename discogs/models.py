"""Pydantic models for Discogs API responses."""

from pydantic import BaseModel, computed_field

DISCOGS_RELEASE_URL_BASE = "https://www.discogs.com/release"


class TrackItem(BaseModel):
    """A single track on a release."""

    position: str
    title: str
    duration: str | None = None
    artists: list[str] = []  # Per-track artists (for compilations)


class ReleaseInfo(BaseModel):
    """Information about a single release containing a track."""

    album: str
    artist: str
    release_id: int
    is_compilation: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def release_url(self) -> str:
        return f"{DISCOGS_RELEASE_URL_BASE}/{self.release_id}"


class TrackReleasesResponse(BaseModel):
    """Response for finding all releases containing a track."""

    track: str
    artist: str | None = None
    releases: list[ReleaseInfo] = []
    total: int = 0
    cached: bool = False


class ReleaseMetadataResponse(BaseModel):
    """Full release metadata from Discogs."""

    release_id: int
    title: str
    artist: str
    year: int | None = None
    label: str | None = None
    artist_id: int | None = None
    label_id: int | None = None
    genres: list[str] = []
    styles: list[str] = []
    tracklist: list[TrackItem] = []
    artwork_url: str | None = None
    cached: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def release_url(self) -> str:
        return f"{DISCOGS_RELEASE_URL_BASE}/{self.release_id}"


class DiscogsSearchRequest(BaseModel):
    """Request for general Discogs search."""

    artist: str | None = None
    album: str | None = None
    track: str | None = None


class DiscogsSearchResult(BaseModel):
    """A single result from Discogs search."""

    album: str | None = None
    artist: str | None = None
    release_id: int
    artwork_url: str | None = None
    confidence: float = 0.0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def release_url(self) -> str:
        return f"{DISCOGS_RELEASE_URL_BASE}/{self.release_id}"


class DiscogsSearchResponse(BaseModel):
    """Response for general Discogs search."""

    results: list[DiscogsSearchResult] = []
    total: int = 0
    cached: bool = False
