"""Main application entry point with dependency injection and lifespan management."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from wxyc_fastapi.healthcheck import liveness_router
from wxyc_fastapi.observability import flush_posthog, init_sentry, shutdown_posthog

from config.settings import get_settings
from core.dependencies import close_http_client
from core.groq_tracing import install_groq_retry_breadcrumbs
from core.logging import setup_logging
from routers.admin import router as admin_router
from routers.health import build_readiness_router
from routers.parse import router as parse_router
from routers.request import router as request_router

load_dotenv()

settings = get_settings()

init_sentry(
    dsn=settings.sentry_dsn,
    service_name="request-o-matic",
    environment=settings.deployment_environment,
    release=settings.app_version,
)

install_groq_retry_breadcrumbs()

# Configure logging
# In production, log to /app/logs which is writable by appuser
log_file = None
if settings.log_level != "DEBUG":
    log_dir = Path("/app/logs") if Path("/app/logs").exists() else Path("logs")
    log_file = log_dir / "request-o-matic.log"
setup_logging(level=settings.log_level, log_file=log_file)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifespan with proper startup and shutdown."""
    # Startup
    logger.info(f"Starting {settings.app_name} v{settings.app_version}")
    logger.info(f"Log level: {settings.log_level}")
    logger.info(
        f"Slack integration: {'enabled' if settings.enable_slack_integration else 'disabled'}"
    )
    logger.info(f"Lookup delegation: {'enabled' if settings.lookup_service_url else 'disabled'}")

    yield

    # Shutdown
    logger.info("Shutting down application")
    shutdown_posthog()
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


# Include routers - health check at root, others versioned.
# Liveness + readiness come from wxyc_fastapi (shared across LML, rom, semantic-index);
# the probe functions still live locally in routers/health.py.
app.include_router(liveness_router, tags=["health"])
app.include_router(build_readiness_router(), tags=["health"])

# V1 API (new)
app.include_router(parse_router, prefix="/api/v1", tags=["parse"])
app.include_router(request_router, prefix="/api/v1", tags=["request"])

# Backwards compatibility - mount at root as well
app.include_router(parse_router, prefix="", tags=["parse-legacy"])
app.include_router(request_router, prefix="", tags=["request-legacy"])

# Admin API for request-line ban management (#151). Mounted at root so the
# routes are reachable as ``/admin/bans`` — operators don't think about the
# ``/api/v1`` prefix for admin tooling.
app.include_router(admin_router)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )
