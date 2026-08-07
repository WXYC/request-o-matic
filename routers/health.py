"""Health check probes wired into wxyc_fastapi's shared healthcheck routers.

The routing and status aggregation live in
[`wxyc_fastapi.healthcheck`](https://github.com/WXYC/wxyc-fastapi/blob/main/src/wxyc_fastapi/healthcheck/__init__.py);
this module only defines the rom-side probe functions and a small
:func:`build_readiness_router` helper that binds them into the shared
``readiness_router`` factory.

Probe contract (per ``wxyc_fastapi.healthcheck.Check``): each probe is an
async callable returning ``"ok"`` on success. Returning any other string or
raising is reported as ``"unavailable"``; exceeding the per-probe timeout is
reported as ``"timeout"``.

Required vs optional:

* ``lookup`` — ``required=True``. Search delegation is rom's core path;
  failure here makes the readiness response ``unhealthy`` (HTTP 503) so the
  orchestrator can route traffic away.
* ``groq`` — ``required=False``. Groq parsing has a ``parsing_unavailable``
  degraded mode; the listener's message still reaches Slack without it.
* ``slack`` — ``required=False``. ``/request`` returns 502 if Slack itself
  is down, but readiness reports ``degraded`` so the dashboard surfaces the
  impairment without removing the container from rotation.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter
from wxyc_fastapi.healthcheck import Check, readiness_router

from config.settings import Settings, get_settings
from core.dependencies import (
    get_cached_slack_webhook_url,
    get_http_client,
    get_slack_bot_config,
)


async def probe_groq(settings: Settings, http_client: httpx.AsyncClient) -> str:
    """Verify the Groq API key is valid and the service is reachable."""
    if not settings.groq_api_key:
        return "unavailable"
    try:
        resp = await http_client.get(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {settings.groq_api_key}"},
        )
    except Exception:
        return "unavailable"
    return "ok" if resp.status_code == 200 else "unavailable"


async def probe_lookup(settings: Settings, http_client: httpx.AsyncClient) -> str:
    """Probe the library-metadata-lookup ``/lookup`` endpoint with bearer auth.

    POSTs a ``raw_message``-only request to ``{lookup_service_url}/lookup``
    carrying the Bearer token (when ``LML_API_KEY`` is set). This is the same
    endpoint ``/request`` hits, so a 401/403 from auth misconfig surfaces here
    instead of silently passing a connectivity ping.

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
    except Exception:
        return "unavailable"
    return "ok" if resp.status_code == 200 else "unavailable"


async def probe_slack(settings: Settings, http_client: httpx.AsyncClient) -> str:
    """Verify the active Slack transport is configured and reachable.

    Follows whichever transport is selected (request-o-matic#215): when
    ``slack_use_bot_token`` is set, ``get_slack_webhook_url`` deliberately
    never resolves a webhook, so this probe checks the bot-token config
    instead of the (always-empty) cached webhook URL -- otherwise the probe
    would report a permanent false ``unavailable`` once the flag flips.

    Bot-token transport: ``ok`` iff ``get_slack_bot_config`` resolves (bot
    token + channel id both set). No live call to Slack is made here, mirroring
    how ``get_slack_bot_config`` itself only checks configuration completeness.

    Webhook transport (flag off): unchanged from before -- sends an empty
    JSON body; Slack returns 400 (missing text) which proves the URL is valid
    and the endpoint is alive.
    """
    if settings.slack_use_bot_token:
        bot_config = await get_slack_bot_config(settings)
        return "ok" if bot_config is not None else "unavailable"

    webhook_url = get_cached_slack_webhook_url()
    if webhook_url is None:
        return "unavailable"
    try:
        resp = await http_client.post(webhook_url, json={})
    except Exception:
        return "unavailable"
    # 400 means Slack received the request but rejected the empty payload --
    # that's exactly what we want: proof the webhook URL is valid.
    return "ok" if resp.status_code in (200, 400) else "unavailable"


def build_readiness_router() -> APIRouter:
    """Build a ``GET /health/ready`` router with rom's three probes.

    Probes resolve :class:`Settings` and the shared :class:`httpx.AsyncClient`
    from the rom-side module singletons (``get_settings()`` / ``get_http_client()``)
    at call time, so no FastAPI dependency injection plumbing is needed for the
    probe arguments. The shared router owns the timeout and aggregation
    behavior.
    """

    async def _groq() -> str:
        return await probe_groq(get_settings(), await get_http_client())

    async def _lookup() -> str:
        return await probe_lookup(get_settings(), await get_http_client())

    async def _slack() -> str:
        return await probe_slack(get_settings(), await get_http_client())

    return readiness_router(
        [
            Check(name="groq", probe=_groq, required=False),
            Check(name="lookup", probe=_lookup, required=True),
            Check(name="slack", probe=_slack, required=False),
        ]
    )
