"""FastAPI dependency injection providers."""

from __future__ import annotations

import hmac
import logging
from typing import TYPE_CHECKING

import httpx
from fastapi import Depends, Header, HTTPException
from groq import AsyncGroq
from wxyc_fastapi.http import async_singleton
from wxyc_fastapi.observability import get_posthog_client as _shared_posthog_client

from config.settings import Settings, get_settings
from core.exceptions import ServiceInitializationError, SlackPostError
from services.ban_admin_client import BanAdminClient
from services.ban_check_client import BanCheckClient
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


async def get_ban_check_client(
    settings: Settings = Depends(get_settings),
    http_client: httpx.AsyncClient = Depends(get_http_client),
) -> BanCheckClient | None:
    """Get the BS ban-check client if request-line ban enforcement is enabled.

    Returns None when either the feature flag (``ENFORCE_REQUEST_BANS``) is off
    or the BS URL (``BS_CHECK_REQUEST_BAN_URL``) is unset, so the router can
    skip the check entirely without a second branch on every request.
    """
    if not settings.enforce_request_bans or not settings.bs_check_request_ban_url:
        return None
    return BanCheckClient(settings.bs_check_request_ban_url, http_client)


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

    if settings.slack_use_bot_token:
        # Bot-token transport is active (request-o-matic#215): skip webhook
        # resolution entirely so a missing/unreachable SLACK_WEBHOOK_KEY_URL
        # can't fail /request with a 500 while a fully-configured bot
        # transport sits unused.
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


async def get_slack_bot_config(
    settings: Settings = Depends(get_settings),
) -> tuple[str, str] | None:
    """Resolve the bot-token Slack transport config (request-o-matic#215).

    Gated on both ``enable_slack_integration`` and ``slack_use_bot_token`` so it
    behaves identically to ``get_slack_webhook_url`` under those flags. Returns
    None (rather than raising) when the flag is on but ``slack_bot_token`` /
    ``slack_channel_id`` aren't both set, so the caller fails closed instead of
    posting with a blank channel.
    """
    if not settings.enable_slack_integration or not settings.slack_use_bot_token:
        return None
    if not settings.slack_bot_token or not settings.slack_channel_id:
        logger.error("SLACK_USE_BOT_TOKEN is set but SLACK_BOT_TOKEN/SLACK_CHANNEL_ID is missing")
        return None
    return settings.slack_bot_token, settings.slack_channel_id


class SlackService:
    """Service for posting messages to Slack.

    Two mutually exclusive transports, selected by which arguments are
    supplied: the legacy incoming webhook (``webhook_url``) or bot-token
    ``chat.postMessage`` (``bot_token`` + ``channel_id``, request-o-matic#215).
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        webhook_url: str | None = None,
        bot_token: str | None = None,
        channel_id: str | None = None,
    ):
        self.http_client = http_client
        self.webhook_url = webhook_url
        self.bot_token = bot_token
        self.channel_id = channel_id

    async def post_blocks(self, blocks: list[dict]) -> str | None:
        """Post message blocks to Slack.

        Args:
            blocks: Slack message blocks

        Returns:
            The posted message's ``ts`` when using the bot-token transport,
            otherwise None (the incoming webhook has no message handle).

        Raises:
            httpx.HTTPError: If the HTTP request itself fails.
            SlackPostError: If the bot-token transport returns
                ``{"ok": false}`` (a 200 that Slack still reports as a
                failure -- ``chat.postMessage`` does not use HTTP status
                codes for API-level errors).
        """
        if self.bot_token is not None:
            return await self._post_via_bot_token(blocks)

        if self.webhook_url is None:
            raise SlackPostError("SlackService has neither a webhook_url nor a bot_token")
        response = await self.http_client.post(self.webhook_url, json={"blocks": blocks})
        response.raise_for_status()
        logger.info("Posted to Slack successfully")
        return None

    async def _post_via_bot_token(self, blocks: list[dict]) -> str:
        response = await self.http_client.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {self.bot_token}"},
            json={"channel": self.channel_id, "blocks": blocks},
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            error = data.get("error", "unknown_error")
            if error == "not_in_channel":
                raise SlackPostError(
                    f"Slack chat.postMessage failed: not_in_channel "
                    f"(bot is not a member of channel {self.channel_id}; invite it with /invite)"
                )
            raise SlackPostError(f"Slack chat.postMessage failed: {error}")
        logger.info("Posted to Slack successfully")
        ts: str = data["ts"]
        return ts


async def get_slack_service(
    settings: Settings = Depends(get_settings),
    webhook_url: str | None = Depends(get_slack_webhook_url),
    bot_config: tuple[str, str] | None = Depends(get_slack_bot_config),
    http_client: httpx.AsyncClient = Depends(get_http_client),
) -> SlackService | None:
    """Get Slack service instance.

    Args:
        settings: Application settings (selects the transport)
        webhook_url: Slack webhook URL (legacy transport)
        bot_config: (bot_token, channel_id) pair (request-o-matic#215 transport)
        http_client: HTTP client

    Returns:
        Optional[SlackService]: Slack service if enabled, None otherwise
    """
    if settings.slack_use_bot_token:
        if bot_config is None:
            return None
        bot_token, channel_id = bot_config
        return SlackService(http_client, bot_token=bot_token, channel_id=channel_id)

    if webhook_url is None:
        return None
    return SlackService(http_client, webhook_url=webhook_url)


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

    parts = authorization.strip().split(None, 1)
    # Encode to bytes so non-ASCII bearer values (which CPython's
    # hmac.compare_digest rejects with TypeError on str) compare safely
    # rather than escaping as an unhandled 500.
    if (
        len(parts) != 2
        or parts[0].lower() != "bearer"
        or not hmac.compare_digest(parts[1].encode("utf-8"), settings.admin_token.encode("utf-8"))
    ):
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
