"""FastAPI dependency injection providers."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx
from fastapi import Depends, Header, HTTPException
from groq import AsyncGroq
from wxyc_fastapi.http import async_singleton
from wxyc_fastapi.observability import get_posthog_client as _shared_posthog_client

from config.settings import Settings, get_settings
from core.exceptions import ServiceInitializationError
from services.ban_admin_client import BanAdminClient
from services.lookup_client import LookupServiceClient

if TYPE_CHECKING:
    from posthog import Posthog

logger = logging.getLogger(__name__)

_slack_webhook_url: str | None = None


async def _make_http_client() -> httpx.AsyncClient:
    """Construct the shared httpx.AsyncClient used across rom services."""
    return httpx.AsyncClient(timeout=30.0)


# Lazy singleton: ``async_singleton`` wraps ``_make_http_client`` with a
# double-check-lock so concurrent first-callers see one factory invocation
# (LML#241 / LML#242 — the FD-leak race the helper exists to prevent).
get_http_client, close_http_client = async_singleton(_make_http_client)


def get_groq_client(settings: Settings = Depends(get_settings)) -> AsyncGroq:
    """Get Groq client instance.

    Args:
        settings: Application settings

    Returns:
        AsyncGroq: Async Groq client instance

    Raises:
        ServiceInitializationError: If Groq API key is not configured
    """
    if not settings.groq_api_key:
        raise ServiceInitializationError("GROQ_API_KEY not configured")
    return AsyncGroq(api_key=settings.groq_api_key, max_retries=4)


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
    return LookupServiceClient(
        settings.lookup_service_url,
        http_client,
        api_key=settings.lml_api_key,
    )


def get_posthog_client(settings: Settings = Depends(get_settings)) -> Posthog | None:
    """Get PostHog client instance, gated on the ``ENABLE_TELEMETRY`` flag.

    The shared ``wxyc_fastapi`` singleton handles the missing-API-key warn-once
    behavior; this wrapper short-circuits when telemetry is disabled entirely.
    """
    if not settings.enable_telemetry:
        logger.debug("Telemetry disabled")
        return None
    return _shared_posthog_client(event_prefix="request")


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


def require_admin_token(
    settings: Settings = Depends(get_settings),
    authorization: str | None = Header(None),
) -> None:
    """Validate ``Authorization: Bearer <ADMIN_TOKEN>`` for admin endpoints.

    Mirrors LML's ``routers/admin.py:_validate_auth`` so operators don't need
    to learn two different bearer-token shapes between services. Status codes:

    * ``ADMIN_TOKEN`` unset on the server -> 403 (fail-closed; the operator
      should hear "disabled", not "you sent the wrong token").
    * No ``Authorization`` header -> 401.
    * Malformed scheme or wrong token -> 403.
    * Correct bearer -> pass.
    """
    if not settings.admin_token:
        raise HTTPException(
            status_code=403,
            detail="Admin endpoint disabled (no ADMIN_TOKEN set)",
        )

    if not authorization:
        raise HTTPException(status_code=401, detail="Missing authorization")

    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or parts[1] != settings.admin_token:
        raise HTTPException(status_code=403, detail="Invalid token")


async def get_ban_admin_client(
    settings: Settings = Depends(get_settings),
    http_client: httpx.AsyncClient = Depends(get_http_client),
) -> BanAdminClient:
    """Build a :class:`BanAdminClient` from settings, or 503 if misconfigured.

    Both ``BS_INTERNAL_BANS_URL`` and ``BS_INTERNAL_KEY`` are required to talk
    to Backend-Service. Surfacing the misconfiguration as 503 (not 500) tells
    operators "the upstream isn't wired" rather than implying a bug in the
    request they sent.
    """
    if not settings.bs_internal_bans_url or not settings.bs_internal_key:
        raise HTTPException(
            status_code=503,
            detail=(
                "Ban admin upstream not configured: set BS_INTERNAL_BANS_URL and BS_INTERNAL_KEY"
            ),
        )
    return BanAdminClient(
        settings.bs_internal_bans_url,
        http_client,
        internal_key=settings.bs_internal_key,
    )
