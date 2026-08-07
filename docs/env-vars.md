# Environment Variables

Required:
- `GROQ_API_KEY` - For AI parsing
- `LOOKUP_SERVICE_URL` - Base URL of library-metadata-lookup service (e.g., `https://library-metadata-lookup-staging.up.railway.app/api/v1`). All search is delegated to this service. If unset, requests still post to Slack via the `search_unavailable` degraded mode (see "Degraded Modes" in [`docs/architecture.md`](architecture.md)).
- `LML_API_KEY` - Bearer token sent on every call to LML. Required when LML has `LML_REQUIRE_AUTH=true` (production). Without it, `/lookup` calls 401 and `/request` returns 502.

Optional:
- `SLACK_WEBHOOK_URL` - For posting results via the legacy incoming webhook (used when `SLACK_USE_BOT_TOKEN` is off)
- `SLACK_WEBHOOK_KEY_URL` - Railway endpoint to fetch Slack webhook key (webhook transport only)
- `SLACK_USE_BOT_TOKEN` - Feature flag (default: `false`). When `true`, `SlackService.post_blocks` posts via `chat.postMessage` with `SLACK_BOT_TOKEN` instead of the incoming webhook, returning the message `ts` (request-o-matic#215). Rendered output is identical either way; the webhook path stays byte-for-byte unchanged while this is off, so it can be flipped back without a deploy if the bot-token path regresses.
- `SLACK_BOT_TOKEN` - Bot token (`xoxb-...`) for `chat.postMessage`. Required when `SLACK_USE_BOT_TOKEN=true`; the app has `chat:write` but not `chat:write.public`, so the bot must be `/invite`d into any channel it posts to, or every post fails with `not_in_channel`.
- `SLACK_CHANNEL_ID` - Channel ID `chat.postMessage` posts to. Required when `SLACK_USE_BOT_TOKEN=true`.
- `SENTRY_DSN` - For error tracking and 100% transaction tracing (Sentry). The Sentry environment tag is read from `RAILWAY_ENVIRONMENT_NAME` (Railway sets this automatically) or `DEPLOYMENT_ENVIRONMENT` if you want to override it; falls back to `local` when neither is set. Outbound calls to LML carry `sentry-trace` headers via the `HttpxIntegration`, so request-o-matic and LML spans link up into a single distributed trace.
- `POSTHOG_API_KEY` - PostHog project API key for telemetry
- `POSTHOG_HOST` - PostHog host URL (default: `https://us.i.posthog.com`)
- `ENABLE_SLACK_INTEGRATION` - Enable/disable Slack notifications (default: `true`)
- `ENABLE_TELEMETRY` - Enable/disable PostHog telemetry (default: `true`)
- `ENABLE_SERVER_TIMING` - Enable/disable the `Server-Timing` response header on `POST /request` (default: `true`). See "Server-Timing header" below.

Admin API for request-line bans (see [`docs/admin-bans.md`](admin-bans.md)):
- `ADMIN_TOKEN` - Bearer token gating the `/admin/bans` endpoints. When unset, every admin request is rejected with 403 (fail-closed). Rotate by updating the Railway service variable.
- `BS_INTERNAL_BANS_URL` - Base URL of Backend-Service's `/internal/banned-fingerprints` CRUD (BS#1261). Example: `https://api.wxyc.org/internal/banned-fingerprints`. When unset, `/admin/bans` returns 503.
- `BS_INTERNAL_KEY` - Shared secret forwarded as `X-Internal-Key` on calls to BS internal endpoints. Must equal `ROM_INTERNAL_KEY` on the BS side. Used by `/admin/bans` (#151); the public `/auth/check-request-ban` handler does NOT consume this.

Request-line ban enforcement ([WXYC/request-o-matic#150](https://github.com/WXYC/request-o-matic/issues/150) + [WXYC/Backend-Service#1261](https://github.com/WXYC/Backend-Service/issues/1261)):
- `ENFORCE_REQUEST_BANS` - Feature flag for request-line ban enforcement. Default `false` so the code can deploy before iOS 3.2 reaches App Store rollout. When `true` AND `BS_CHECK_REQUEST_BAN_URL` is set, every `POST /request` consults BS before parsing.
- `BS_CHECK_REQUEST_BAN_URL` - Full URL of Backend-Service's `POST /auth/check-request-ban` endpoint (apps/auth service, port 8082), e.g. `https://wxyc-auth-staging.up.railway.app/auth/check-request-ban`. When unset, the ban check is disabled regardless of `ENFORCE_REQUEST_BANS`.

Flow when enforcement is on and the caller supplies `Authorization: Bearer <jwt>` and/or `X-Device-Fingerprint: <uuid>`: ROM POSTs both headers to `BS_CHECK_REQUEST_BAN_URL` before parse. `banned: true` short-circuits to 403 (no Slack, no Groq, no LML) and emits a `request_blocked` PostHog event with `user_id`, `fingerprint`, `ban_reason`, `ban_source`. `banned: false` proceeds. BS 401 (invalid JWT) and 404 (user not found) proceed-as-unauth — the caller MUST NOT see a 401 on `POST /request`, since v3.1 iOS clients send no Authorization header. When BS is unreachable, ROM fails open: log a Sentry breadcrumb, emit `degraded_mode=ban_check_unavailable` telemetry, proceed.

User-Agent gate ([WXYC/request-o-matic#155](https://github.com/WXYC/request-o-matic/issues/155)):
- `STRICT_FINGERPRINT_FOR_KNOWN_CLIENTS` - Feature flag for the User-Agent gate. Default `false`. When `true`, requests whose `User-Agent` identifies a registered WXYC client at-or-above its strict-mode version (currently `WXYC-iOS >= 3.2`) are rejected `403` if `X-Device-Fingerprint` is absent or is not a well-formed UUID (the header is normalized through `services/fingerprint.normalize_fingerprint`, so a malformed value is treated exactly like a missing one -- a real 3.2+ client always sends its Keychain UUID, so garbage here is evasion). Unknown UAs (curl, browsers, v3.1 iOS, anything not registered in `services/ua_gate.py`) are unaffected — the existing lenient contract still holds. The gate is independent of `ENFORCE_REQUEST_BANS`: it's a structural check on the request shape from known clients, not a ban decision. Rejection emits a `request_blocked` PostHog event with `ban_source='rom_strict_mode'` and the received `user_agent`. `ban_reason` distinguishes the two rejection causes ([WXYC/request-o-matic#226](https://github.com/WXYC/request-o-matic/issues/226)): `ua_gate_missing_fingerprint` when `X-Device-Fingerprint` was absent, or `ua_gate_malformed_fingerprint` when it was present but not a well-formed UUID -- the malformed case additionally carries `fingerprint_prefix` (an 8-character redacted prefix, matching `_redact_fingerprint` in `routers/admin.py`) and `fingerprint_length` (the raw header length as an integer). The raw header value is never emitted.

Operator flip sequence (after iOS 3.2 ships):
1. Deploy ROM with the gate code (default-off).
2. Verify iOS 3.2 fingerprint pipeline against staging.
3. Wait for App Store rollout to reach ~90% (App Store Connect → Analytics → App Versions).
4. Flip `STRICT_FINGERPRINT_FOR_KNOWN_CLIENTS=true` on staging, watch PostHog for `request_blocked` events keyed on `ban_source='rom_strict_mode'`. Spikes here indicate either a 3.2 fingerprint regression or active evasion attempts; a slow trickle is expected.
5. Flip on production. Rollback recipe: set the var to `false` and restart; legitimate traffic recovers within one restart cycle.

## Server-Timing header ([Backend-Service#881](https://github.com/WXYC/Backend-Service/issues/881))

When `ENABLE_SERVER_TIMING=true` (default), `POST /request` attaches a [`Server-Timing`](https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Server-Timing) response header that merges ROM's own per-stage timings (`parse`, `lookup_service`, `slack_post`) with the sub-stage breakdown LML forwards in *its* `Server-Timing` header (`library_search`, `metadata_enrichment`, `discogs`, and others). LML's own `total` is renamed to `lml_total` (not dropped) and ROM appends its own `total`, so the header carries exactly one ROM-owned `total`, last — e.g. `parse;dur=3.1, lookup_service;dur=8560, slack_post;dur=42, library_search;dur=41, metadata_enrichment;dur=8500, discogs;dur=806, lml_total;dur=8551.4, total;dur=8610`. Comparing `lookup_service` against `lml_total` isolates the ROM<->LML transport + LML framework overhead that neither side's own timing otherwise explains.

This surfaces the same `RequestTelemetry.track_step` durations ROM and LML already ship to PostHog — no new capture layer — so a caller (e.g. the `lookup` CLI) can attribute a slow round-trip to a named server stage (the motivating case: an ~8.5s `metadata_enrichment` Apple-Music-probe stall the JSON body never revealed). It is purely additive/out-of-band: the response body is byte-identical whether the flag is on or off, so it is safe to toggle in production. Set `ENABLE_SERVER_TIMING=false` as the kill switch.

Implementation: the shared serializer is `RequestTelemetry.as_server_timing` in [wxyc-fastapi](https://github.com/WXYC/wxyc-fastapi) (>=1.1.0); the forward/merge lives in `routers/request._emit_server_timing_header` + `core/server_timing.parse_server_timing`. The building of the header can never raise into the request path (all exceptions are logged and swallowed).
