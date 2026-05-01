"""Health check router with real dependency connectivity checks."""

import asyncio
import logging

import httpx
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from config.settings import Settings, get_settings
from core.dependencies import (
    get_cached_slack_webhook_url,
    get_http_client,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

# Per-check timeout in seconds
CHECK_TIMEOUT = 3.0

# Core services whose failure means "unhealthy"
CORE_SERVICES = {"groq"}


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


async def _check_lookup_service(settings: Settings, http_client: httpx.AsyncClient) -> str:
    """Check the library-metadata-lookup lookup endpoint with auth.

    POSTs a ``raw_message``-only request to ``{lookup_service_url}/lookup``
    carrying the Bearer token (when ``LML_API_KEY`` is set). This is the same
    endpoint /request hits, so a 401/403 from auth misconfig surfaces here
    instead of silently passing a /health connectivity ping.

    LML's ``LookupRequest`` makes every field optional, so ``raw_message``
    alone validates and the orchestrator returns ``200`` with empty results --
    cheap enough to run on every readiness check.
    """
    if not settings.lookup_service_url:
        return "unavailable"
    headers: dict[str, str] = {}
    if settings.lml_api_key:
        headers["Authorization"] = f"Bearer {settings.lml_api_key}"
    try:
        url = f"{settings.lookup_service_url.rstrip('/')}/lookup"
        resp = await http_client.post(
            url,
            json={"raw_message": "readiness-probe"},
            headers=headers,
        )
        return "ok" if resp.status_code == 200 else "error"
    except Exception:
        return "error"


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
    summary="Liveness check",
    description="Shallow liveness probe. Returns 200 immediately with no external calls. Used by Railway's healthcheckPath to mark the container as routable.",
    responses={
        200: {"description": "Service is alive"},
    },
)
async def liveness_check():
    """Shallow liveness probe -- no external calls, instant response."""
    return JSONResponse(content={"status": "ok"})


@router.get(
    "/health/ready",
    summary="Readiness check",
    description="Deep readiness probe. Checks connectivity to Groq, lookup service, and Slack.",
    responses={
        200: {"description": "Service is healthy or degraded"},
        503: {"description": "Service is unhealthy (core dependency down)"},
    },
)
async def readiness_check(
    settings: Settings = Depends(get_settings),
    http_client: httpx.AsyncClient = Depends(get_http_client),
):
    """Health check with real connectivity probes for every dependency."""

    results = await asyncio.gather(
        _run_check(_check_groq(settings, http_client)),
        _run_check(_check_lookup_service(settings, http_client)),
        _run_check(_check_slack(http_client)),
    )

    services = {
        "groq": results[0],
        "lookup": results[1],
        "slack": results[2],
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
