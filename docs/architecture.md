# Architecture

## Request Flow
1. **Parse**: Groq AI (`llama-3.1-8b-instant`) extracts artist/song/album from message
2. **Early return**: Non-request messages (feedback, DJ messages) are posted to Slack without search
3. **Delegate**: Search is delegated to [library-metadata-lookup](https://github.com/WXYC/library-metadata-lookup) via HTTP (`LOOKUP_SERVICE_URL`).
4. **Slack**: Post enriched results with artwork to Slack

## Degraded Modes
Slack is the only hard dependency. When Groq or LML are unavailable the listener's message still reaches Slack, with a context line explaining what's missing. The response is `200` with a `degraded_mode` field:

- **`parsing_unavailable`** — Groq failed (rate limit, timeout, parse error, etc.). The raw listener message is posted to Slack with a `_Parsing unavailable_` context line. No classification, no search.
- **`search_unavailable`** — LML is down or `LOOKUP_SERVICE_URL` is unset. The parsed message is posted to Slack with a `_Search unavailable_` context line containing any artist/song/album fields Groq extracted. Empty `library_results`.

If Slack itself fails the endpoint returns `502` — there is no further fallback. PostHog events for degraded requests carry `degraded_mode` and `degraded_reason` (the exception class name) so outages are visible in telemetry.

## Key Files
- `models.py` - Re-exports `LibraryItem` (alias for `LibraryCatalogItem`) and `ReleaseMetadata` (alias for `DiscogsMatchResult`) from `generated.api_models`, plus the `preview_url(metadata)` helper for streaming-priority logic.
- `generated/api_models.py` - Pydantic v2 models generated from `wxyc-shared/api.yaml`. Do not edit by hand; regenerate via `bash scripts/generate_api_models.sh`. The script prefers a sibling `wxyc-shared/` directory and falls back to downloading `api.yaml` from GitHub.
- `tests/factories.py` - `make_library_item` / `make_release_metadata` factories that supply the now-required `call_number`, `library_url`, and `release_url` fields with sensible defaults.
- `routers/request.py` - Request handling: parse, delegate to lookup service, post to Slack
- `routers/health.py` - Health check endpoints: `GET /health` (shallow liveness probe, no external calls) and `GET /health/ready` (deep readiness check: groq, lookup, slack services). Railway's `healthcheckPath` uses `/health` so the container becomes routable immediately on boot.
- `services/parser.py` - Groq AI message parsing
- `services/lookup_client.py` - HTTP client for library-metadata-lookup delegation
- `services/slack.py` - Slack message formatting and posting
- `core/dependencies.py` - FastAPI dependency injection (HTTP client, Groq, Slack, PostHog). Sentry, telemetry, cache stats, and PostHog client construction live in [`wxyc-fastapi`](https://github.com/WXYC/wxyc-fastapi); `core/dependencies.get_posthog_client` only wraps the shared singleton with the rom-side `enable_telemetry` flag.
- `core/groq_tracing.py` - Sentry instrumentation for Groq parse calls: a `groq_parse_span` context manager wrapping `services.parser.parse_request` (op `ai.parse`, tagged with model, input length, token counts, and parse-result fields), and an `install_groq_retry_breadcrumbs()` filter on the `groq._base_client` logger that converts the SDK's silent 429-retry log lines into structured `groq.retry` breadcrumbs. Installed once from `main.py` after `init_sentry`.
- `config/settings.py` - Pydantic Settings configuration

## Discogs Cache (Optional)
The service supports an optional PostgreSQL cache for Discogs data to reduce API calls:

**Cache Strategy:**
1. Query local PostgreSQL cache first
2. On cache miss, query Discogs API
3. Write API results back to cache for future queries
4. Gracefully degrade to API-only if cache unavailable

**Cache Service (`discogs/cache_service.py`):**
- Uses asyncpg for async PostgreSQL connections
- Trigram similarity (pg_trgm) for fuzzy text matching
- `CacheUnavailableError` exception for connection failures

**Enabling the Cache:**
Set `DATABASE_URL_DISCOGS` environment variable to a PostgreSQL connection URL. If not set, the service uses Discogs API directly (existing behavior).

**Setting Up the Cache Database:**
The cache ETL pipeline lives in a separate repo: [WXYC/discogs-etl](https://github.com/WXYC/discogs-etl). See that repo for setup instructions. The SQL schema files in `discogs-etl/schema/` define the shared contract between the ETL pipeline and this service's `cache_service.py`.

## Library ETL
The `library.db` SQLite database is synced daily from the WXYC MySQL database (Kattare) by the ETL pipeline in [WXYC/discogs-etl](https://github.com/WXYC/discogs-etl). The pipeline queries MySQL via the MariaDB `mysql` CLI (required for MySQL 4.1 old-password auth), converts the TSV output to SQLite with FTS5 via `scripts/tsv_to_sqlite.py`, enriches it with streaming links from `streaming_availability.db` (a GitHub Release artifact built by library-metadata-lookup's streaming availability pipeline), and uploads the result to the LML staging and production services via `POST /admin/upload-library-db`. See `discogs-etl/scripts/sync-library.sh` and `discogs-etl/.github/workflows/sync-library.yml` for details.

## Common Issues and Fixes

Search logic (artist matching, ambiguous format handling, compilation search) lives in the [library-metadata-lookup](https://github.com/WXYC/library-metadata-lookup) service. See that repo's documentation for details on false positive filtering, ambiguous format handling, and compilation search.
