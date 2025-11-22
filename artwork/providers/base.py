from typing import Protocol

from artwork.models import ArtworkRequest, SearchResult


class ArtworkProvider(Protocol):
    """Protocol for artwork providers."""

    @property
    def name(self) -> str:
        """Provider name for attribution."""
        ...

    async def search(self, request: ArtworkRequest) -> list[SearchResult]:
        """
        Search for album artwork matching the request.

        Args:
            request: The artwork request containing song/album/artist info.

        Returns:
            List of search results, ordered by relevance.
            Empty list if no results found.
        """
        ...
