"""Request handling router that delegates search to library-metadata-lookup service.

Flow:
1. (Optional) Consult BS /auth/check-request-ban to enforce request-line bans.
2. Parse the message using Groq AI to extract artist/song/album
3. Early return for non-requests (feedback, DJ messages, etc.)
4. Delegate search to library-metadata-lookup service via HTTP
5. Post enriched results to Slack

Degraded modes:
- "parsing_unavailable": Groq parsing failed. Slack receives the raw listener
  message with a "_Parsing unavailable_" note. No classification, no search.
- "search_unavailable": LML is down or unconfigured. Slack receives the parsed
  metadata with a "_Search unavailable_" note. No library results.
- "ban_check_unavailable": BS /auth/check-request-ban was unreachable. The
  request still proceeds (fail-open) but the telemetry property is set so
  operators can see the outage in PostHog.

Slack remains the only hard dependency: if it fails, the endpoint returns 502.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx
import sentry_sdk
from fastapi import APIRouter, Depends, Header, HTTPException, Response
from groq import AsyncGroq
from pydantic import BaseModel
from wxyc_fastapi.observability import RequestTelemetry, get_cache_stats, init_cache_stats

from config.settings import Settings, get_settings
from core.dependencies import (
    SlackService,
    get_ban_check_client,
    get_groq_client,
    get_lookup_client,
    get_posthog_client,
    get_slack_service,
)
from core.server_timing import parse_server_timing

if TYPE_CHECKING:
    from posthog import Posthog
from models import LibraryItem, ReleaseMetadata
from services.ban_check_client import BanCheckClient, BanCheckUnavailableError
from services.fingerprint import normalize_fingerprint
from services.lookup_client import LookupRequest, LookupServiceClient
from services.parser import MessageType, ParsedRequest, parse_request
from services.slack import build_simple_slack_blocks, build_slack_blocks, build_slack_metadata
from services.ua_gate import (
    UA_GATE_BAN_REASON,
    UA_GATE_BAN_SOURCE,
    is_known_strict_client,
)

logger = logging.getLogger(__name__)


# Friendly labels for message types in Slack
MESSAGE_TYPE_LABELS = {
    MessageType.REQUEST: "Song Request",
    MessageType.DJ_MESSAGE: "Message to DJ",
    MessageType.FEEDBACK: "Feedback",
    MessageType.OTHER: "Other",
}

DEGRADED_PARSING = "parsing_unavailable"
DEGRADED_SEARCH = "search_unavailable"
DEGRADED_BAN_CHECK = "ban_check_unavailable"

_LML_TRANSIENT_ERRORS = (
    httpx.HTTPStatusError,
    httpx.ConnectError,
    httpx.TimeoutException,
)

router = APIRouter(tags=["request"])


class RequestBody(BaseModel):
    """Request body for song request parsing."""

    message: str
    skip_slack: bool = False
    skip_cache: bool = False


class UnifiedResponse(BaseModel):
    """Combined response from parsing, artwork lookup, and library search."""

    parsed: ParsedRequest | None = None
    artwork: ReleaseMetadata | None = None
    library_results: list[LibraryItem] = []
    result_artworks: list[ReleaseMetadata | None] = []
    # Search metadata
    search_type: str = "none"
    song_not_found: bool = False
    found_on_compilation: bool = False
    context_message: str | None = None
    cache_stats: dict | None = None
    degraded_mode: str | None = None


def _parsed_context_parts(parsed: ParsedRequest) -> list[str]:
    parts: list[str] = []
    if parsed.artist:
        parts.append(f"Artist: {parsed.artist}")
    if parsed.album:
        parts.append(f"Album: {parsed.album}")
    if parsed.song:
        parts.append(f"Song: {parsed.song}")
    return parts


async def post_results_to_slack(
    slack_service: SlackService | None,
    message: str,
    parsed: ParsedRequest,
    items_with_artwork: list[tuple[LibraryItem, ReleaseMetadata | None]],
    context: str | None = None,
    fingerprint: str | None = None,
) -> None:
    """Post formatted results to Slack.

    Args:
        slack_service: Slack service instance
        message: Original request message
        parsed: Parsed request
        items_with_artwork: Library items with their artwork
        context: Optional context message
        fingerprint: Requester's ``X-Device-Fingerprint``, if any. Carried as
            private ``chat.postMessage`` metadata (request-o-matic#209) --
            never rendered into the blocks themselves.

    Raises:
        HTTPException: If posting to Slack fails
    """
    if not slack_service:
        logger.info("Slack integration disabled, skipping post")
        return

    if items_with_artwork:
        blocks = build_slack_blocks(message, items_with_artwork, context)
    elif not parsed.is_request:
        label = MESSAGE_TYPE_LABELS.get(parsed.message_type, "Other")
        blocks = build_simple_slack_blocks(message, f"_{label}_")
    else:
        # Request but no results found
        ctx = " | ".join(_parsed_context_parts(parsed)) or None
        blocks = build_simple_slack_blocks(message, f"_No results found_ {ctx or ''}")

    try:
        await slack_service.post_blocks(blocks, metadata=build_slack_metadata(fingerprint))
    except Exception as e:
        logger.error(f"Failed to post to Slack: {e}")
        raise HTTPException(status_code=502, detail=f"Failed to post to Slack: {e}") from e


async def _post_degraded_to_slack(
    slack_service: SlackService | None,
    message: str,
    parsed: ParsedRequest | None,
    note: str,
    fingerprint: str | None = None,
) -> None:
    if not slack_service:
        logger.info("Slack integration disabled, skipping degraded post")
        return

    context_segments = [f"_{note}_"]
    if parsed is not None:
        context_segments.extend(_parsed_context_parts(parsed))
    context = " | ".join(context_segments)
    blocks = build_simple_slack_blocks(message, context)

    try:
        await slack_service.post_blocks(blocks, metadata=build_slack_metadata(fingerprint))
    except Exception as e:
        logger.error(f"Failed to post degraded message to Slack: {e}")
        raise HTTPException(status_code=502, detail=f"Failed to post to Slack: {e}") from e


def _emit_server_timing_header(
    http_response: Response,
    telemetry: RequestTelemetry,
    *,
    enabled: bool,
    lml_server_timing: str | None = None,
) -> None:
    """Attach a Server-Timing header from rom's telemetry + forwarded LML stages.

    Merges the sub-stages LML returned in its own ``Server-Timing`` header into
    rom's per-stage timings, so a caller (e.g. the ``lookup`` CLI) can attribute
    a slow ``/request`` round-trip to a named server stage (Backend-Service#881).
    LML's own self-measured ``total`` is forwarded too, renamed to
    ``lml_total`` — comparing it against rom's ``lookup_service`` leg isolates
    the rom<->LML transport + LML framework overhead that neither side's own
    timing otherwise explains.

    Flag-gated and fully defensive: a disabled flag skips it, and any failure to
    build the header is logged and swallowed so this observability path can never
    break the request.
    """
    if not enabled:
        return
    try:
        # Forwarded LML legs become ``extra`` legs in ``as_server_timing`` —
        # appended after rom's own steps and before rom's canonical ``total``.
        # Rename LML's ``total`` to ``lml_total`` (rather than dropping it) so
        # both totals survive. Drop any leg whose *emitted* key collides with a
        # rom step (rom's own measurement wins) so the header stays single-total
        # and strict-parser-safe. Guarding the post-rename key — not the raw
        # forwarded name — is what actually future-proofs against a future rom
        # step named ``lml_total`` (rom/LML stage names are otherwise disjoint).
        extra: dict[str, float] = {}
        for name, dur in parse_server_timing(lml_server_timing):
            key = "lml_total" if name == "total" else name
            if key in telemetry.steps:
                continue
            extra[key] = dur
        http_response.headers["Server-Timing"] = telemetry.as_server_timing(extra=extra or None)
    except Exception:
        logger.warning("Failed to build Server-Timing header", exc_info=True)


@router.post(
    "/request",
    response_model=UnifiedResponse,
    summary="Process song request",
    description="""
    Complete workflow: parse song request, search library, find artwork, and post to Slack.

    This endpoint:
    1. Parses the message using AI to extract song/album/artist
    2. Searches the library catalog for matches
    3. Fetches album artwork from external providers
    4. Posts enriched results to Slack
    5. Returns combined results

    Example request:
    ```json
    {
        "message": "Play Bohemian Rhapsody by Queen"
    }
    ```

    The response includes:
    - Parsed metadata (song, artist, album, request type)
    - Library search results with catalog info
    - Album artwork URLs where available
    - degraded_mode field when an upstream dependency was unavailable
      ("parsing_unavailable" if Groq failed; "search_unavailable" if LML failed)
    """,
    responses={
        200: {"description": "Request processed (possibly via a degraded fallback)"},
        400: {"description": "Invalid request (empty message)"},
        403: {"description": "Caller is banned from the request line"},
        502: {"description": "Slack unavailable"},
    },
)
async def handle_request(
    request: RequestBody,
    http_response: Response,
    groq_client: AsyncGroq = Depends(get_groq_client),
    slack_service: SlackService | None = Depends(get_slack_service),
    posthog_client: Posthog | None = Depends(get_posthog_client),
    lookup_client: LookupServiceClient | None = Depends(get_lookup_client),
    ban_check_client: BanCheckClient | None = Depends(get_ban_check_client),
    settings: Settings = Depends(get_settings),
    authorization: str | None = Header(default=None),
    x_device_fingerprint: str | None = Header(default=None),
    user_agent: str | None = Header(default=None),
):
    """Parse a request, search the library, and post to Slack.

    On Groq or LML failure, fall back to posting the raw / parsed message with
    a degraded-mode note rather than returning an error.
    """
    if not request.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    init_cache_stats(extra_keys=["memory_misses"])
    telemetry = RequestTelemetry(
        api_call_keys=["groq", "discogs", "slack"],
        distinct_id="request-o-matic-service",
        event_prefix="request",
    )

    # User-Agent gate (WXYC/request-o-matic#155). Runs BEFORE the BS ban check
    # so a known-client request missing its fingerprint is rejected without an
    # extra BS round-trip. Unknown UAs (v3.1 iOS, browsers, curl, anything not
    # in the registry) keep the existing lenient behavior — see services/ua_gate.py.
    # The gate is independent of ENFORCE_REQUEST_BANS: it's a structural check
    # on the request shape from known clients, not a ban decision.
    if (
        settings.strict_fingerprint_for_known_clients
        and not x_device_fingerprint
        and is_known_strict_client(user_agent)
    ):
        if posthog_client is not None:
            # Wrap PostHog capture in the same try/except as the BS-ban path
            # below — a PostHog outage must not flip the response away from 403.
            try:
                posthog_client.capture(
                    distinct_id="request-o-matic-service",
                    event="request_blocked",
                    properties={
                        "user_id": None,
                        "fingerprint": None,
                        "ban_reason": UA_GATE_BAN_REASON,
                        "ban_source": UA_GATE_BAN_SOURCE,
                        "user_agent": user_agent,
                    },
                )
            except Exception:
                logger.exception("PostHog request_blocked capture failed (ua_gate)")
        logger.info(
            "Blocked known-client request missing X-Device-Fingerprint (user_agent=%s)",
            user_agent,
        )
        raise HTTPException(status_code=403, detail="Request blocked")

    # Request-line ban enforcement (WXYC/request-o-matic#150 + BS#1261).
    # Runs BEFORE parse so an abusive listener does not consume Groq TPM or
    # LML cache budget. The check is fully skipped when:
    #   - the feature flag is off (ban_check_client is None), OR
    #   - the caller supplies neither Authorization nor X-Device-Fingerprint
    #     (matches v3.1 iOS clients in production — BS would 400 no_signal).
    # The BS endpoint is public; we do NOT forward X-Internal-Key here.
    # Gate on the *normalized* fingerprint, not the raw header: check() drops a
    # malformed value and then raises ValueError when no signal survives, which
    # nothing here catches. A caller whose only header was e.g.
    # `X-Device-Fingerprint: not-a-uuid` therefore got a 500 instead of the
    # no-signal skip. The UA gate above deliberately stays on the raw header —
    # it asserts presence, not validity.
    ban_check_fingerprint = normalize_fingerprint(x_device_fingerprint)
    ban_check_degraded = False
    if ban_check_client is not None and (authorization or ban_check_fingerprint):
        try:
            ban_result = await ban_check_client.check(
                authorization=authorization,
                fingerprint=ban_check_fingerprint,
            )
        except BanCheckUnavailableError as exc:
            # Fail open: log a Sentry breadcrumb so the outage is visible in
            # the distributed trace, and let the request proceed. The
            # degraded_mode flag rides on the final telemetry event.
            logger.warning("Ban check unavailable (%s); proceeding with request", exc)
            sentry_sdk.add_breadcrumb(
                category="ban_check",
                level="warning",
                message="BS /auth/check-request-ban unavailable; failing open",
                data={"error": str(exc)},
            )
            ban_check_degraded = True
        else:
            if ban_result.banned:
                # Shadow-ban semantics: no Slack, no Groq, no LML. iOS v3.2
                # silently swallows 403 so the listener sees nothing.
                # PostHog capture is wrapped because a Posthog ingest outage
                # raising here would otherwise prevent the 403 and 500 the
                # caller instead — breaking shadow-ban (the banned listener
                # would see that the backend is doing extra work).
                if posthog_client is not None:
                    try:
                        posthog_client.capture(
                            distinct_id="request-o-matic-service",
                            event="request_blocked",
                            properties={
                                "user_id": ban_result.user_id,
                                "fingerprint": ban_result.fingerprint,
                                "ban_reason": ban_result.ban_reason,
                                "ban_source": ban_result.ban_source,
                            },
                        )
                    except Exception:
                        logger.exception("PostHog request_blocked capture failed")
                logger.info(
                    "Blocked request from banned caller (source=%s, user_id=%s)",
                    ban_result.ban_source,
                    ban_result.user_id,
                )
                raise HTTPException(status_code=403, detail="Request blocked")

    try:
        with telemetry.track_step("parse"):
            telemetry.record_api_call("groq")
            parsed = await parse_request(request.message, groq_client)
            logger.info(
                f"Parsed request: is_request={parsed.is_request}, type={parsed.message_type}"
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(
            "Parsing unavailable (%s: %s); posting raw message to Slack",
            type(e).__name__,
            e,
        )
        if not request.skip_slack:
            telemetry.record_api_call("slack")
            await _post_degraded_to_slack(
                slack_service,
                request.message,
                parsed=None,
                note="Parsing unavailable",
                fingerprint=x_device_fingerprint,
            )
        if posthog_client:
            parse_props: dict = {
                "results_count": 0,
                "search_type": "none",
                "had_artist": False,
                "had_album": False,
                "had_song": False,
                "is_request": None,
                "message_type": None,
                "degraded_mode": DEGRADED_PARSING,
                "degraded_reason": type(e).__name__,
            }
            if ban_check_degraded:
                parse_props["ban_check_degraded"] = True
            telemetry.send_to_posthog(
                posthog_client,
                parse_props,
            )
        _emit_server_timing_header(http_response, telemetry, enabled=settings.enable_server_timing)
        return UnifiedResponse(
            parsed=None,
            cache_stats=get_cache_stats(),
            degraded_mode=DEGRADED_PARSING,
        )

    if not parsed.is_request:
        if not request.skip_slack:
            await post_results_to_slack(
                slack_service,
                request.message,
                parsed,
                [],
                context=None,
                fingerprint=x_device_fingerprint,
            )
        _emit_server_timing_header(http_response, telemetry, enabled=settings.enable_server_timing)
        return UnifiedResponse(
            parsed=parsed,
            cache_stats=get_cache_stats(),
        )

    library_results: list[LibraryItem] = []
    items_with_artwork: list[tuple[LibraryItem, ReleaseMetadata | None]] = []
    song_not_found = False
    found_on_compilation = False
    context: str | None = None
    cache_stats_override: dict | None = None
    search_type = "none"
    lookup_response = None
    lookup_failure: Exception | None = None
    # Sentinel: the LML-outage path below (lookup_client is None, or the call
    # raises) skips the assignment inside the try, so this must be bound before
    # the merged-header emit at the final return — else an UnboundLocalError 500s
    # the degraded path instead of returning the graceful search-unavailable 200.
    lml_server_timing: str | None = None

    if lookup_client is None:
        # Treat missing LOOKUP_SERVICE_URL as a permanent LML outage rather than
        # a hard 503. Operators see the degraded_mode signal in telemetry.
        logger.warning("LOOKUP_SERVICE_URL not configured; entering search-degraded mode")
        lookup_failure = RuntimeError("LOOKUP_SERVICE_URL not configured")
    else:
        with telemetry.track_step("lookup_service"):
            lookup_request = LookupRequest(
                artist=parsed.artist,
                song=parsed.song,
                album=parsed.album,
                raw_message=request.message,
            )
            try:
                lookup_result = await lookup_client.lookup(
                    lookup_request, skip_cache=request.skip_cache
                )
                lookup_response = lookup_result.response
                lml_server_timing = lookup_result.server_timing
            except _LML_TRANSIENT_ERRORS as e:
                logger.warning(
                    "Search unavailable (%s: %s); posting parsed message to Slack",
                    type(e).__name__,
                    e,
                )
                lookup_failure = e

    if lookup_response is not None:
        # Apply corrected artist for Slack display
        if lookup_response.corrected_artist:
            parsed.artist = lookup_response.corrected_artist

        # Extract results for Slack posting. Generated LookupResponse fields
        # are nullable in the schema; coerce to non-null defaults for callers.
        results = lookup_response.results or []
        # Exclude non-library results from the request channel. LML surfaces
        # albums not in the WXYC catalog as row-less items (LML#631: id=0, empty
        # library_url, call_number="(external)"). A DJ can't pull a non-shelved
        # album, so these don't belong in the request feed. When this empties the
        # list, the Slack post falls through to the existing "No results found"
        # branch in post_results_to_slack.
        results = [item for item in results if item.library_item.id > 0]
        library_results = [item.library_item for item in results]
        items_with_artwork = [(item.library_item, item.artwork) for item in results]
        search_type = str(lookup_response.search_type or "none")
        song_not_found = bool(lookup_response.song_not_found)
        found_on_compilation = bool(lookup_response.found_on_compilation)
        context = lookup_response.context_message

        # Prefer the lookup service's cache stats over local counters,
        # which stay at 0 since all Discogs/cache work is delegated.
        # wxyc-shared tightened LookupResponse.cache_stats from a free-form
        # object to the structured CacheStats model, so a parsed response
        # carries a CacheStats here; flatten it back to a plain dict for the
        # UnifiedResponse wire shape (and tolerate a raw dict defensively).
        stats = lookup_response.cache_stats
        if stats:
            cache_stats_override = stats if isinstance(stats, dict) else stats.model_dump()

    with telemetry.track_step("slack_post"):
        if not request.skip_slack:
            telemetry.record_api_call("slack")
            if lookup_failure is not None:
                await _post_degraded_to_slack(
                    slack_service,
                    request.message,
                    parsed=parsed,
                    note="Search unavailable",
                    fingerprint=x_device_fingerprint,
                )
            else:
                await post_results_to_slack(
                    slack_service,
                    request.message,
                    parsed,
                    items_with_artwork,
                    context,
                    fingerprint=x_device_fingerprint,
                )

    # Extract main artwork from first result
    artwork = (
        next((art for _, art in items_with_artwork if art), None) if items_with_artwork else None
    )

    # Send telemetry. degraded_mode priority: search > ban_check. Search is
    # more user-visible (no library results) so it wins on the single field;
    # ban_check_degraded rides on its own dedicated property so operators can
    # see both signals in PostHog.
    degraded_mode = DEGRADED_SEARCH if lookup_failure is not None else None
    if degraded_mode is None and ban_check_degraded:
        degraded_mode = DEGRADED_BAN_CHECK
    if posthog_client:
        properties: dict = {
            "results_count": len(library_results),
            "search_type": search_type,
            "had_artist": bool(parsed.artist),
            "had_album": bool(parsed.album),
            "had_song": bool(parsed.song),
            "is_request": parsed.is_request,
            "message_type": parsed.message_type.value if parsed.message_type else None,
        }
        if degraded_mode:
            properties["degraded_mode"] = degraded_mode
            if lookup_failure is not None:
                properties["degraded_reason"] = type(lookup_failure).__name__
        if ban_check_degraded:
            # Always emit the ban_check signal independently so it surfaces
            # even when a more severe degraded_mode (search) is also set.
            properties["ban_check_degraded"] = True
        telemetry.send_to_posthog(posthog_client, properties)

    _emit_server_timing_header(
        http_response,
        telemetry,
        enabled=settings.enable_server_timing,
        lml_server_timing=lml_server_timing,
    )
    return UnifiedResponse(
        parsed=parsed,
        artwork=artwork,
        library_results=library_results,
        result_artworks=[art for _, art in items_with_artwork],
        search_type=search_type,
        song_not_found=song_not_found,
        found_on_compilation=found_on_compilation,
        context_message=context,
        cache_stats=cache_stats_override or get_cache_stats(),
        degraded_mode=degraded_mode,
    )
