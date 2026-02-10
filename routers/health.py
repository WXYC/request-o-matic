"""Health check router with real dependency connectivity checks."""

import asyncio
import logging

import httpx
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from config.settings import Settings, get_settings
from core.dependencies import (
    get_cached_slack_webhook_url,
    get_discogs_service,
    get_http_client,
    get_library_db,
)
from discogs.service import DiscogsService
from library.db import LibraryDB

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

# Per-check timeout in seconds
CHECK_TIMEOUT = 3.0

# Core services whose failure means "unhealthy"
CORE_SERVICES = {"groq", "database"}


async def _check_database(db: LibraryDB) -> str:
    """Ping the SQLite database."""
    return "ok" if await db.is_available() else "error"


async def _check_groq(settings: Settings, http_client: httpx.AsyncClient) -> str:
    """Verify the Groq API key is valid and the service is reachable."""
    if not settings.groq_api_key:
        return "error"
    try:
        resp = await http_client.get(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {settings.groq_api_key}"},
        )
        return "ok" if resp.status_code == 200 else "error"
    except Exception:
        return "error"


async def _check_discogs_api(discogs_service: DiscogsService | None) -> str:
    """Ping the Discogs API via the service's own client."""
    if discogs_service is None:
        return "unavailable"
    return "ok" if await discogs_service.check_api() else "error"


async def _check_discogs_cache(discogs_service: DiscogsService | None) -> str:
    """Ping the PostgreSQL cache pool."""
    if discogs_service is None or discogs_service.cache_service is None:
        return "unavailable"
    return "ok" if await discogs_service.cache_service.is_available() else "error"


async def _check_slack(http_client: httpx.AsyncClient) -> str:
    """Verify the Slack webhook URL is reachable.

    Sends an empty JSON body; Slack returns 400 (missing text) which proves
    the URL is valid and the endpoint is alive.
    """
    webhook_url = get_cached_slack_webhook_url()
    if webhook_url is None:
        return "unavailable"
    try:
        resp = await http_client.post(webhook_url, json={})
        # 400 means Slack received the request but rejected the empty payload --
        # that's exactly what we want: proof the webhook URL is valid.
        return "ok" if resp.status_code in (200, 400) else "error"
    except Exception:
        return "error"


async def _run_check(coro) -> str:
    """Run a single health check with a timeout."""
    try:
        return await asyncio.wait_for(coro, timeout=CHECK_TIMEOUT)
    except TimeoutError:
        return "timeout"


@router.get(
    "/health",
    summary="Health check",
    description="Check the health status of the application and its services",
    responses={
        200: {"description": "Service is healthy or degraded"},
        503: {"description": "Service is unhealthy (core dependency down)"},
    },
)
async def health_check(
    settings: Settings = Depends(get_settings),
    db: LibraryDB = Depends(get_library_db),
    discogs_service: DiscogsService | None = Depends(get_discogs_service),
    http_client: httpx.AsyncClient = Depends(get_http_client),
):
    """Health check with real connectivity probes for every dependency."""

    results = await asyncio.gather(
        _run_check(_check_groq(settings, http_client)),
        _run_check(_check_database(db)),
        _run_check(_check_discogs_api(discogs_service)),
        _run_check(_check_discogs_cache(discogs_service)),
        _run_check(_check_slack(http_client)),
    )

    services = {
        "groq": results[0],
        "database": results[1],
        "discogs_api": results[2],
        "discogs_cache": results[3],
        "slack": results[4],
    }

    # Determine overall status
    core_ok = all(services[s] == "ok" for s in CORE_SERVICES)
    all_configured_ok = all(v in ("ok", "unavailable") for v in services.values())

    if core_ok and all_configured_ok:
        status = "healthy"
    elif core_ok:
        status = "degraded"
    else:
        status = "unhealthy"

    body = {
        "status": status,
        "version": settings.app_version,
        "services": services,
    }

    status_code = 200 if status in ("healthy", "degraded") else 503
    return JSONResponse(content=body, status_code=status_code)
