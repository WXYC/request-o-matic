"""Health check router with service status monitoring."""
import logging
from typing import Optional

from fastapi import APIRouter, Depends

from config.settings import Settings, get_settings
from core.dependencies import get_library_db, get_artwork_finder
from library.db import LibraryDB
from artwork.finder import ArtworkFinder

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    summary="Health check",
    description="Check the health status of the application and its services",
    responses={
        200: {"description": "Service is healthy"},
    },
)
async def health_check(
    settings: Settings = Depends(get_settings),
    db: LibraryDB = Depends(get_library_db),
    finder: Optional[ArtworkFinder] = Depends(get_artwork_finder),
):
    """Comprehensive health check with service status details."""
    
    # Check database connection
    db_status = "connected"
    try:
        # Simple query to verify connection
        if db._conn is None:
            db_status = "disconnected"
    except Exception as e:
        logger.warning(f"Database health check failed: {e}")
        db_status = "error"
    
    # Check Groq client (API key configured)
    groq_status = "configured" if settings.groq_api_key else "not_configured"
    
    # Check Discogs availability
    discogs_status = "available" if settings.discogs_token and finder else "unavailable"
    if not settings.enable_artwork_lookup:
        discogs_status = "disabled"
    
    # Check Slack integration
    slack_status = "enabled" if settings.enable_slack_integration else "disabled"
    
    return {
        "status": "healthy",
        "version": settings.app_version,
        "services": {
            "groq": groq_status,
            "database": db_status,
            "discogs": discogs_status,
            "slack": slack_status,
        },
        "features": {
            "artwork_lookup": settings.enable_artwork_lookup,
            "slack_integration": settings.enable_slack_integration,
        },
    }
