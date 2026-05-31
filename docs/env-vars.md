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
- `BS_INTERNAL_KEY` - Shared secret forwarded as `X-Internal-Key` on calls to BS internal endpoints. Must equal `ROM_INTERNAL_KEY` on the BS side. Shared with the request-time ban-check client.
