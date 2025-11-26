"""FastAPI dependency injection providers."""
import logging
from functools import lru_cache
from typing import Optional

import httpx
from fastapi import Depends
from groq import Groq

from artwork.finder import ArtworkFinder
from artwork.providers.discogs import DiscogsProvider
from config.settings import Settings, get_settings
from core.exceptions import ServiceInitializationError
from library.db import LibraryDB

logger = logging.getLogger(__name__)

# Module-level instances for lifecycle management
_http_client: Optional[httpx.AsyncClient] = None
_library_db: Optional[LibraryDB] = None


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
            _library_db = LibraryDB(db_path=settings.library_db_path)
            await _library_db.connect()
            logger.info(f"Library database connected: {settings.library_db_path}")
        except Exception as e:
            logger.error(f"Failed to initialize library database: {e}")
            raise ServiceInitializationError(f"Database initialization failed: {e}")
    
    return _library_db


async def close_library_db() -> None:
    """Close library database connection."""
    global _library_db
    if _library_db:
        await _library_db.close()
        _library_db = None


async def get_artwork_finder(settings: Settings = Depends(get_settings)) -> Optional[ArtworkFinder]:
    """Get artwork finder instance with configured providers.
    
    Args:
        settings: Application settings
        
    Returns:
        Optional[ArtworkFinder]: Artwork finder if enabled, None otherwise
    """
    if not settings.enable_artwork_lookup:
        logger.info("Artwork lookup disabled")
        return None
    
    providers = []
    
    if settings.discogs_token:
        providers.append(DiscogsProvider(settings.discogs_token))
        logger.debug("Discogs provider initialized")
    else:
        logger.warning("DISCOGS_TOKEN not set - Discogs provider disabled")
    
    if not providers:
        logger.warning("No artwork providers configured")
        return None
    
    return ArtworkFinder(providers)


async def get_slack_webhook_url(
    settings: Settings = Depends(get_settings),
    http_client: httpx.AsyncClient = Depends(get_http_client),
) -> Optional[str]:
    """Get Slack webhook URL from settings or Railway endpoint.
    
    Args:
        settings: Application settings
        http_client: HTTP client for fetching from Railway
        
    Returns:
        Optional[str]: Slack webhook URL if configured and enabled
        
    Raises:
        ServiceInitializationError: If fetching webhook URL fails
    """
    if not settings.enable_slack_integration:
        logger.info("Slack integration disabled")
        return None
    
    # Check for webhook URL in settings
    if settings.slack_webhook_url:
        logger.info("Using Slack webhook URL from environment")
        return settings.slack_webhook_url
    
    # Fetch from Railway endpoint
    try:
        response = await http_client.get(
            "https://wxyc-requests-endpoint-production.up.railway.app"
        )
        response.raise_for_status()
        webhook_key = response.text.strip()
        webhook_url = f"https://hooks.slack.com/services/{webhook_key}"
        logger.info("Slack webhook URL configured from Railway")
        return webhook_url
    except Exception as e:
        logger.error(f"Failed to fetch Slack webhook key: {e}")
        raise ServiceInitializationError(f"Failed to fetch Slack webhook key: {e}")


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
        response = await self.http_client.post(
            self.webhook_url,
            json={"blocks": blocks}
        )
        response.raise_for_status()
        logger.info("Posted to Slack successfully")


async def get_slack_service(
    webhook_url: Optional[str] = Depends(get_slack_webhook_url),
    http_client: httpx.AsyncClient = Depends(get_http_client),
) -> Optional[SlackService]:
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

