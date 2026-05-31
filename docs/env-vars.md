# Environment Variables

Required:
- `GROQ_API_KEY` - For AI parsing
- `LOOKUP_SERVICE_URL` - Base URL of library-metadata-lookup service (e.g., `https://library-metadata-lookup-staging.up.railway.app/api/v1`). All search is delegated to this service. If unset, requests still post to Slack via the `search_unavailable` degraded mode (see "Degraded Modes" in [`docs/architecture.md`](architecture.md)).
- `LML_API_KEY` - Bearer token sent on every call to LML. Required when LML has `LML_REQUIRE_AUTH=true` (production). Without it, `/lookup` calls 401 and `/request` returns 502.

Optional:
- `SLACK_WEBHOOK_URL` - For posting results
- `SLACK_WEBHOOK_KEY_URL` - Railway endpoint to fetch Slack webhook key
- `SENTRY_DSN` - For error tracking and 100% transaction tracing (Sentry). The Sentry environment tag is read from `RAILWAY_ENVIRONMENT_NAME` (Railway sets this automatically) or `DEPLOYMENT_ENVIRONMENT` if you want to override it; falls back to `local` when neither is set. Outbound calls to LML carry `sentry-trace` headers via the `HttpxIntegration`, so request-o-matic and LML spans link up into a single distributed trace.
- `POSTHOG_API_KEY` - PostHog project API key for telemetry
- `POSTHOG_HOST` - PostHog host URL (default: `https://us.i.posthog.com`)
- `ENABLE_SLACK_INTEGRATION` - Enable/disable Slack notifications (default: `true`)
- `ENABLE_TELEMETRY` - Enable/disable PostHog telemetry (default: `true`)

Admin API for request-line bans (see [`docs/admin-bans.md`](admin-bans.md)):
- `ADMIN_TOKEN` - Bearer token gating the `/admin/bans` endpoints. When unset, every admin request is rejected with 403 (fail-closed). Rotate by updating the Railway service variable.
- `BS_INTERNAL_BANS_URL` - Base URL of Backend-Service's `/internal/banned-fingerprints` CRUD (BS#1261). Example: `https://api.wxyc.org/internal/banned-fingerprints`. When unset, `/admin/bans` returns 503.
- `BS_INTERNAL_KEY` - Shared secret forwarded as `X-Internal-Key` on calls to BS internal endpoints. Must equal `ROM_INTERNAL_KEY` on the BS side. Used by `/admin/bans` (#151); the public `/auth/check-request-ban` handler does NOT consume this.

Request-line ban enforcement ([WXYC/request-o-matic#150](https://github.com/WXYC/request-o-matic/issues/150) + [WXYC/Backend-Service#1261](https://github.com/WXYC/Backend-Service/issues/1261)):
- `ENFORCE_REQUEST_BANS` - Feature flag for request-line ban enforcement. Default `false` so the code can deploy before iOS 3.2 reaches App Store rollout. When `true` AND `BS_CHECK_REQUEST_BAN_URL` is set, every `POST /request` consults BS before parsing.
- `BS_CHECK_REQUEST_BAN_URL` - Full URL of Backend-Service's `POST /auth/check-request-ban` endpoint (apps/auth service, port 8082), e.g. `https://wxyc-auth-staging.up.railway.app/auth/check-request-ban`. When unset, the ban check is disabled regardless of `ENFORCE_REQUEST_BANS`.

Flow when enforcement is on and the caller supplies `Authorization: Bearer <jwt>` and/or `X-Device-Fingerprint: <uuid>`: ROM POSTs both headers to `BS_CHECK_REQUEST_BAN_URL` before parse. `banned: true` short-circuits to 403 (no Slack, no Groq, no LML) and emits a `request_blocked` PostHog event with `user_id`, `fingerprint`, `ban_reason`, `ban_source`. `banned: false` proceeds. BS 401 (invalid JWT) and 404 (user not found) proceed-as-unauth — the caller MUST NOT see a 401 on `POST /request`, since v3.1 iOS clients send no Authorization header. When BS is unreachable, ROM fails open: log a Sentry breadcrumb, emit `degraded_mode=ban_check_unavailable` telemetry, proceed.
