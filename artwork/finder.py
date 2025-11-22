import logging
from typing import Optional

from artwork.models import ArtworkRequest, ArtworkResponse, SearchResult
from artwork.providers.base import ArtworkProvider

logger = logging.getLogger(__name__)


class ArtworkFinder:
    """Orchestrates artwork search across multiple providers."""

    def __init__(self, providers: list[ArtworkProvider]):
        self.providers = providers

    async def find(self, request: ArtworkRequest) -> ArtworkResponse:
        """
        Find artwork for the given request.

        Tries each provider in order and returns the best result
        based on confidence score.
        """
        if not request.song and not request.album and not request.artist:
            logger.warning("Empty request - no fields to search")
            return ArtworkResponse()

        all_results: list[SearchResult] = []

        for provider in self.providers:
            try:
                results = await provider.search(request)
                all_results.extend(results)
                logger.info(f"Provider {provider.name} returned {len(results)} results")
            except Exception as e:
                logger.error(f"Provider {provider.name} failed: {e}")
                continue

        if not all_results:
            logger.info("No artwork found from any provider")
            return ArtworkResponse()

        # Sort by confidence and return the best match
        all_results.sort(key=lambda r: r.confidence, reverse=True)
        best = all_results[0]

        logger.info(
            f"Best match: {best.artist} - {best.album} "
            f"(confidence: {best.confidence:.2f}, source: {best.source})"
        )

        return ArtworkResponse(
            artwork_url=best.artwork_url,
            release_url=best.release_url,
            album=best.album,
            artist=best.artist,
            source=best.source,
            confidence=best.confidence,
        )
