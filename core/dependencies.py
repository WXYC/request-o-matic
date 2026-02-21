"""FastAPI dependency injection providers."""

import logging

import asyncpg
import httpx
from fastapi import Depends
from groq import Groq
from posthog import Posthog

from config.settings import Settings, get_settings
from core.exceptions import ServiceInitializationError
from discogs.cache_service import DiscogsCacheService
from discogs.service import DiscogsService
from library.db import LibraryDB
from services.lookup_client import LookupServiceClient

logger = logging.getLogger(__name__)

# Module-level instances for lifecycle management
_http_client: httpx.AsyncClient | None = None
_library_db: LibraryDB | None = None
_discogs_service: DiscogsService | None = None
_discogs_pool: asyncpg.Pool | None = None
_posthog_client: Posthog | None = None
_slack_webhook_url: str | None = None


async def get_http_client() -> httpx.AsyncClient:
    """Get or create HTTP client for async requests.

    Returns:
        httpx.AsyncClient: Shared async HTTP client
    """
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=30.0)
    return _http_client


async def close_http_client() -> None:
    """Close the HTTP client."""
    global _http_client
    if _http_client:
        await _http_client.aclose()
        _http_client = None


def get_groq_client(settings: Settings = Depends(get_settings)) -> Groq:
    """Get Groq client instance.

    Args:
        settings: Application settings

    Returns:
        Groq: Groq client instance

    Raises:
        ServiceInitializationError: If Groq API key is not configured
    """
    if not settings.groq_api_key:
        raise ServiceInitializationError("GROQ_API_KEY not configured")
    return Groq(api_key=settings.groq_api_key)


async def get_library_db(settings: Settings = Depends(get_settings)) -> LibraryDB:
    """Get library database instance.

    Args:
        settings: Application settings

    Returns:
        LibraryDB: Connected library database instance

    Raises:
        ServiceInitializationError: If database initialization fails
    """
    global _library_db

    if _library_db is None:
        try:
            db_path = settings.resolved_library_db_path
            _library_db = LibraryDB(db_path=db_path)
            await _library_db.connect()
            logger.info(f"Library database connected: {db_path}")
        except Exception as e:
            logger.error(f"Failed to initialize library database: {e}")
            raise ServiceInitializationError(f"Database initialization failed: {e}") from e

    return _library_db


async def close_library_db() -> None:
    """Close library database connection."""
    global _library_db
    if _library_db:
        await _library_db.close()
        _library_db = None


async def get_discogs_service(
    settings: Settings = Depends(get_settings),
) -> DiscogsService | None:
    """Get Discogs service instance with optional PostgreSQL cache.

    When DATABASE_URL_DISCOGS is configured, creates an asyncpg connection pool
    and wires up DiscogsCacheService for local caching of Discogs data.

    Args:
        settings: Application settings

    Returns:
        Optional[DiscogsService]: Discogs service if configured, None otherwise
    """
    global _discogs_service
    global _discogs_pool

    if not settings.discogs_token:
        logger.debug("DISCOGS_TOKEN not set - Discogs service disabled")
        return None

    if _discogs_service is None:
        cache_service = None

        if settings.database_url_discogs and _discogs_pool is None:
            try:
                _discogs_pool = await asyncpg.create_pool(
                    settings.database_url_discogs, min_size=1, max_size=5
                )
                logger.info("Discogs cache pool connected")
            except Exception as e:
                logger.warning(f"Failed to create Discogs cache pool: {type(e).__name__}: {e}")

        if _discogs_pool is not None:
            cache_service = DiscogsCacheService(_discogs_pool)
            logger.info("Discogs cache service enabled")

        _discogs_service = DiscogsService(settings.discogs_token, cache_service=cache_service)
        logger.info(
            f"Discogs service initialized (cache: {'enabled' if cache_service else 'disabled'})"
        )

    return _discogs_service


async def close_discogs_service() -> None:
    """Close Discogs service, its HTTP client, and the cache pool."""
    global _discogs_service
    global _discogs_pool
    if _discogs_service:
        await _discogs_service.close()
        _discogs_service = None
    if _discogs_pool:
        await _discogs_pool.close()
        _discogs_pool = None


async def get_lookup_client(
    settings: Settings = Depends(get_settings),
    http_client: httpx.AsyncClient = Depends(get_http_client),
) -> LookupServiceClient | None:
    """Get lookup service client if delegation is enabled.

    Args:
        settings: Application settings
        http_client: Shared HTTP client

    Returns:
        LookupServiceClient if LOOKUP_SERVICE_URL is set, None otherwise
    """
    if not settings.lookup_service_url:
        return None
    return LookupServiceClient(settings.lookup_service_url, http_client)


def get_posthog_client(settings: Settings = Depends(get_settings)) -> Posthog | None:
    """Get PostHog client instance.

    Args:
        settings: Application settings

    Returns:
        Optional[Posthog]: PostHog client if configured and enabled, None otherwise
    """
    global _posthog_client

    if not settings.enable_telemetry:
        logger.debug("Telemetry disabled")
        return None

    if not settings.posthog_api_key:
        logger.debug("POSTHOG_API_KEY not set - telemetry disabled")
        return None

    if _posthog_client is None:
        _posthog_client = Posthog(
            project_api_key=settings.posthog_api_key,
            host=settings.posthog_host,
        )
        logger.info(f"PostHog client initialized (host: {settings.posthog_host})")

    return _posthog_client


def flush_posthog() -> None:
    """Flush any buffered PostHog events."""
    global _posthog_client
    if _posthog_client:
        _posthog_client.flush()


def shutdown_posthog() -> None:
    """Shutdown PostHog client gracefully."""
    global _posthog_client
    if _posthog_client:
        _posthog_client.shutdown()
        _posthog_client = None
        logger.info("PostHog client shutdown")


async def get_slack_webhook_url(
    settings: Settings = Depends(get_settings),
    http_client: httpx.AsyncClient = Depends(get_http_client),
) -> str | None:
    """Get Slack webhook URL from settings or Railway endpoint.

    Caches the resolved URL in a module-level variable so that
    ``get_cached_slack_webhook_url()`` can return it without re-fetching.

    Args:
        settings: Application settings
        http_client: HTTP client for fetching from Railway

    Returns:
        Optional[str]: Slack webhook URL if configured and enabled

    Raises:
        ServiceInitializationError: If fetching webhook URL fails
    """
    global _slack_webhook_url

    if not settings.enable_slack_integration:
        logger.info("Slack integration disabled")
        return None

    # Return cached value if already resolved
    if _slack_webhook_url is not None:
        return _slack_webhook_url

    # Check for webhook URL in settings
    if settings.slack_webhook_url:
        logger.info("Using Slack webhook URL from environment")
        _slack_webhook_url = settings.slack_webhook_url
        return _slack_webhook_url

    # Fetch from Railway endpoint
    try:
        response = await http_client.get(settings.slack_webhook_key_url)
        response.raise_for_status()
        webhook_key = response.text.strip()
        webhook_url = f"https://hooks.slack.com/services/{webhook_key}"
        logger.info("Slack webhook URL configured from Railway")
        _slack_webhook_url = webhook_url
        return _slack_webhook_url
    except Exception as e:
        logger.error(f"Failed to fetch Slack webhook key: {e}")
        raise ServiceInitializationError(f"Failed to fetch Slack webhook key: {e}") from e


def get_cached_slack_webhook_url() -> str | None:
    """Return the already-resolved Slack webhook URL, or None.

    This avoids re-fetching from Railway on every health check.
    The URL is set the first time ``get_slack_webhook_url()`` resolves it.
    """
    return _slack_webhook_url


class SlackService:
    """Service for posting messages to Slack."""

    def __init__(self, webhook_url: str, http_client: httpx.AsyncClient):
        self.webhook_url = webhook_url
        self.http_client = http_client

    async def post_blocks(self, blocks: list[dict]) -> None:
        """Post message blocks to Slack.

        Args:
            blocks: Slack message blocks

        Raises:
            httpx.HTTPError: If posting to Slack fails
        """
        response = await self.http_client.post(self.webhook_url, json={"blocks": blocks})
        response.raise_for_status()
        logger.info("Posted to Slack successfully")


async def get_slack_service(
    webhook_url: str | None = Depends(get_slack_webhook_url),
    http_client: httpx.AsyncClient = Depends(get_http_client),
) -> SlackService | None:
    """Get Slack service instance.

    Args:
        webhook_url: Slack webhook URL
        http_client: HTTP client

    Returns:
        Optional[SlackService]: Slack service if enabled, None otherwise
    """
    if webhook_url is None:
        return None
    return SlackService(webhook_url, http_client)
