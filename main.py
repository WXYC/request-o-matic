"""Main application entry point with dependency injection and lifespan management."""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request

from artwork.router import router as artwork_router
from config.settings import get_settings
from core.dependencies import (
    close_discogs_service,
    close_http_client,
    close_library_db,
    flush_posthog,
    shutdown_posthog,
)
from core.logging import setup_logging
from discogs.router import router as discogs_router
from library.router import router as library_router
from routers.health import router as health_router
from routers.parse import router as parse_router
from routers.request import router as request_router

# Load environment variables
load_dotenv()

# Get settings
settings = get_settings()

# Configure logging
# In production, log to /app/logs which is writable by appuser
log_file = None
if settings.log_level != "DEBUG":
    log_dir = Path("/app/logs") if Path("/app/logs").exists() else Path("logs")
    log_file = log_dir / "request-parser.log"
setup_logging(level=settings.log_level, log_file=log_file)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifespan with proper startup and shutdown."""
    # Startup
    logger.info(f"Starting {settings.app_name} v{settings.app_version}")
    logger.info(f"Log level: {settings.log_level}")
    logger.info(f"Slack integration: {'enabled' if settings.enable_slack_integration else 'disabled'}")
    logger.info(f"Artwork lookup: {'enabled' if settings.enable_artwork_lookup else 'disabled'}")
    
    yield
    
    # Shutdown
    logger.info("Shutting down application")
    shutdown_posthog()
    await close_library_db()
    await close_discogs_service()
    await close_http_client()
    logger.info("All services shut down")


app = FastAPI(
    title=settings.app_name,
    description="Supplement song requests with structured metadata, album artwork, and library catalog info",
    version=settings.app_version,
    lifespan=lifespan,
)


@app.middleware("http")
async def posthog_flush_middleware(request: Request, call_next):
    """Flush PostHog events after each request to prevent data loss."""
    response = await call_next(request)
    flush_posthog()
    return response


# Include routers - health check at root, others versioned
app.include_router(health_router, prefix="", tags=["health"])

# V1 API (new)
app.include_router(parse_router, prefix="/api/v1", tags=["parse"])
app.include_router(request_router, prefix="/api/v1", tags=["request"])
app.include_router(artwork_router, prefix="/api/v1", tags=["artwork"])
app.include_router(library_router, prefix="/api/v1", tags=["library"])
app.include_router(discogs_router, prefix="/api/v1", tags=["discogs"])

# Backwards compatibility - mount at root as well
app.include_router(parse_router, prefix="", tags=["parse-legacy"])
app.include_router(request_router, prefix="", tags=["request-legacy"])
app.include_router(artwork_router, prefix="", tags=["artwork-legacy"])
app.include_router(library_router, prefix="", tags=["library-legacy"])

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )
