"""Discogs API integration module.

Provides cached access to Discogs API with FastAPI endpoints:

Endpoints:
    GET  /api/v1/discogs/track-album       Find album containing a track
    GET  /api/v1/discogs/track-releases    Find all releases with a track
    GET  /api/v1/discogs/release/{id}      Get full release metadata
    POST /api/v1/discogs/search            General release search

Caching:
    - Track searches: configurable TTL (default 1 hour), maxsize from settings
    - Release metadata: configurable TTL (default 4 hours), half maxsize
    - Search results: configurable TTL (default 1 hour), maxsize from settings

Configuration (via environment or settings):
    - DISCOGS_TOKEN: Required API token
    - DISCOGS_TRACK_CACHE_TTL: Track cache TTL in seconds
    - DISCOGS_RELEASE_CACHE_TTL: Release cache TTL in seconds
    - DISCOGS_SEARCH_CACHE_TTL: Search cache TTL in seconds
    - DISCOGS_CACHE_MAXSIZE: Maximum cache entries

Usage:
    from discogs.service import DiscogsService
    from discogs.models import TrackAlbumResponse

    service = DiscogsService(token="your-token")
    result = await service.search_track("VI Scose Poise", artist="Autechre")
"""
