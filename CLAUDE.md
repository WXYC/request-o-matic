# Claude Code Instructions for Request-O-Matic

## Project Overview

Request-O-Matic is a FastAPI service for WXYC radio that processes song requests. It parses natural language messages, delegates search to [library-metadata-lookup](https://github.com/WXYC/library-metadata-lookup), and posts enriched results to Slack.

## Architecture

### Request Flow
1. **Parse**: Groq AI (`llama-3.1-8b-instant`) extracts artist/song/album from message
2. **Early return**: Non-request messages (feedback, DJ messages) are posted to Slack without search
3. **Delegate**: Search is delegated to [library-metadata-lookup](https://github.com/WXYC/library-metadata-lookup) via HTTP (`LOOKUP_SERVICE_URL`).
4. **Slack**: Post enriched results with artwork to Slack

### Degraded Modes
Slack is the only hard dependency. When Groq or LML are unavailable the listener's message still reaches Slack, with a context line explaining what's missing. The response is `200` with a `degraded_mode` field:

- **`parsing_unavailable`** — Groq failed (rate limit, timeout, parse error, etc.). The raw listener message is posted to Slack with a `_Parsing unavailable_` context line. No classification, no search.
- **`search_unavailable`** — LML is down or `LOOKUP_SERVICE_URL` is unset. The parsed message is posted to Slack with a `_Search unavailable_` context line containing any artist/song/album fields Groq extracted. Empty `library_results`.

If Slack itself fails the endpoint returns `502` — there is no further fallback. PostHog events for degraded requests carry `degraded_mode` and `degraded_reason` (the exception class name) so outages are visible in telemetry.

### Key Files
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

### Discogs Cache (Optional)
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

### Library ETL
The `library.db` SQLite database is synced daily from the WXYC MySQL database (Kattare) by the ETL pipeline in [WXYC/discogs-etl](https://github.com/WXYC/discogs-etl). The pipeline queries MySQL via the MariaDB `mysql` CLI (required for MySQL 4.1 old-password auth), converts the TSV output to SQLite with FTS5 via `scripts/tsv_to_sqlite.py`, enriches it with streaming links from `streaming_availability.db` (a GitHub Release artifact built by library-metadata-lookup's streaming availability pipeline), and uploads the result to the LML staging and production services via `POST /admin/upload-library-db`. See `discogs-etl/scripts/sync-library.sh` and `discogs-etl/.github/workflows/sync-library.yml` for details.

## Development Workflow

### Branches
- **`main`** - Development branch. Push here to deploy to **staging**.
- **`prod`** - Production branch. Push here to deploy to **production**.

### Typical Flow
1. Develop and test locally (run server with `uvicorn main:app --reload`)
2. Push to `main` to deploy to staging
3. Run integration tests against staging to verify
4. Merge `main` → `prod` to deploy to production

## Testing

### Test Types

| Type | Location | Marker(s) | External Services | Purpose |
|------|----------|-----------|-------------------|---------|
| Unit | `tests/unit/` | (none) | Mocked | Fast, isolated component tests |
| Integration | `tests/integration/` | `external_api` | Real Groq API | End-to-end verification |
| Performance | `tests/performance/` | `external_api`, `slow` | Real Groq API | Response time benchmarks |

### Marker scheme

Markers follow the canonical "architecture A" vocabulary defined in [the WXYC wiki test-patterns doc](https://github.com/WXYC/wiki/blob/main/plans/test-patterns.md), Section 3. Markers route CI by infrastructure; tier (unit / integration / performance) is documented by directory layout. The set of markers used by this repo:

- **`external_api`** — needs network egress and real third-party API keys (Groq, Slack). Default `pytest` deselects them; opt in with `-m "external_api"`.
- **`slow`** — orthogonal cost dimension, takes more than ~10s. Used together with `external_api` on the performance suite. Opted out from the marker-sync check (`# ci-sync-skip: slow ...` in `pyproject.toml`) because the performance suite is run manually against staging/production with `TEST_ENV` set.
- **`contract`** — per-repo addition (legitimate per Section 3, "What is NOT in the marker namespace"). Reserved for tests that verify the *shape* of an external API contract (Slack, Groq) rather than just calling it. Currently no tests use it; the marker is declared so that future contract tests have a stable name. Opted out from the marker-sync check because, by design, it is manual-only.

The reusable check at `WXYC/wxyc-etl/.github/workflows/check-ci-marker-sync.yml` is wired into `ci.yml` as the `marker-sync` job. It guards the invariant that every marker actually used by a test is either selected by some CI invocation or explicitly opted out.

### Unit Tests
Use mocks for all external services (Groq, lookup service). Run frequently during development:
```bash
venv/bin/python -m pytest tests/unit/ -v
```

### External-API Tests
Hit the real Groq API using staging environment variables from Railway. The `conftest.py` automatically loads staging env vars when the Railway CLI is available:
```bash
# Requires RAILWAY_TOKEN_STAGING env var or Railway CLI login
venv/bin/python -m pytest tests/integration/ -v -m external_api

# Or use the helper script
./test-integration.sh -v
```

External-API tests are skipped if required env vars (`GROQ_API_KEY`) are missing.

These tests are **not** part of the auto-CI run on `main`. The suite shares a per-org Groq TPM bucket with the staging container and saturates the 6000 TPM cap on `llama-3.1-8b-instant`, so running it on every push cascades into 429s + timeouts (see [#118](https://github.com/WXYC/request-o-matic/issues/118) for the diagnosis). Trigger it manually from the GitHub Actions UI via the **External API Tests** workflow (`.github/workflows/external-api.yml`); it always targets staging. The `deploy-staging` smoke test in `ci.yml` remains the actual deploy gate.

Two caveats when running the manual workflow: (1) trigger it *after* the `deploy-staging` job finishes if you want the test code and the deployed code to match — the suite hits a fixed staging URL, so it sees whatever was last deployed there, regardless of when you trigger. (2) Pick the same git ref in the workflow-dispatch UI as is currently deployed; the test source comes from the chosen ref while the deployed service comes from `main`. Production is deliberately not an option: `TestFullRequestIntegration` POSTs to `/request`, which posts to Slack from the deployed container, so a prod run would spam the live WXYC channel. To test prod, run pytest locally with `TEST_ENV=production` after disabling Slack posting.

### Test Environment Configuration
Use `TEST_ENV` to control which server external-API and performance tests hit:

```bash
# Test against local server (default) - requires running uvicorn locally
TEST_ENV=local venv/bin/python -m pytest tests/integration/ -v -m external_api

# Test against staging server on Railway
TEST_ENV=staging venv/bin/python -m pytest tests/integration/ -v -m external_api

# Test against production server on Railway
TEST_ENV=production venv/bin/python -m pytest tests/integration/ -v -m external_api
```

| TEST_ENV | URL |
|----------|-----|
| `local` (default) | `http://localhost:8000/api/v1` |
| `staging` | `https://request-o-matic-staging.up.railway.app/api/v1` |
| `production` | `https://request-o-matic-production.up.railway.app/api/v1` |

### Local Server Testing
Spin up a local server to test changes before pushing:
```bash
# Start local server
uvicorn main:app --reload

# Test with lookup script
venv/bin/python scripts/lookup.py --local "song request here"

# Or use the interactive REPL
venv/bin/python scripts/repl.py --local
```

**Note:** External-API and performance tests with `TEST_ENV=local` automatically start and stop the local server, so you don't need to manually run uvicorn first. The test fixture detects if a server is already running and skips startup if so.

### Manual Testing Tools
- **`scripts/lookup.py`** - One-off lookups against production (default) or local (`--local`).
- **`scripts/repl.py`** - Interactive REPL with command history, server switching (`:local`/`:prod`)
- **`scripts/create_posthog_dashboard.py`** - Creates PostHog dashboard for telemetry visualization (requires `POSTHOG_PERSONAL_API_KEY` and `POSTHOG_PROJECT_ID`)

### Bug Fix Protocol
**For every request bug where a lookup fails to find the correct release:**
1. Create a **unit test** in `tests/unit/` that reproduces the bug with mocked data
2. Create an **integration test** in `tests/integration/` (`@pytest.mark.external_api`) that verifies the fix against real APIs
3. The integration test should assert that false positives are excluded AND correct results are included

## Deployment

- Hosted on Railway
- `main` branch auto-deploys to **staging**
- `prod` branch auto-deploys to **production**
- Use `railway` CLI for status/logs (requires TTY for some commands)

## Common Issues and Fixes

Search logic (artist matching, ambiguous format handling, compilation search) lives in the [library-metadata-lookup](https://github.com/WXYC/library-metadata-lookup) service. See that repo's documentation for details on false positive filtering, ambiguous format handling, and compilation search.

## Environment Variables

Required:
- `GROQ_API_KEY` - For AI parsing
- `LOOKUP_SERVICE_URL` - Base URL of library-metadata-lookup service (e.g., `https://library-metadata-lookup-staging.up.railway.app/api/v1`). All search is delegated to this service. If unset, requests still post to Slack via the `search_unavailable` degraded mode (see "Degraded Modes" above).
- `LML_API_KEY` - Bearer token sent on every call to LML. Required when LML has `LML_REQUIRE_AUTH=true` (production). Without it, `/lookup` calls 401 and `/request` returns 502.

Optional:
- `SLACK_WEBHOOK_URL` - For posting results
- `SLACK_WEBHOOK_KEY_URL` - Railway endpoint to fetch Slack webhook key
- `SENTRY_DSN` - For error tracking and 100% transaction tracing (Sentry). The Sentry environment tag is read from `RAILWAY_ENVIRONMENT_NAME` (Railway sets this automatically) or `DEPLOYMENT_ENVIRONMENT` if you want to override it; falls back to `local` when neither is set. Outbound calls to LML carry `sentry-trace` headers via the `HttpxIntegration`, so request-o-matic and LML spans link up into a single distributed trace.
- `POSTHOG_API_KEY` - PostHog project API key for telemetry
- `POSTHOG_HOST` - PostHog host URL (default: `https://us.i.posthog.com`)
- `ENABLE_SLACK_INTEGRATION` - Enable/disable Slack notifications (default: `true`)
- `ENABLE_TELEMETRY` - Enable/disable PostHog telemetry (default: `true`)

## Code Style

- Line length: 100 chars
- Use `black` for formatting, `ruff` for linting
- Type hints encouraged but not enforced
- Async/await for all I/O operations

## Example Music Data for Tests

WXYC is a freeform station. When creating test fixtures or mock data, use representative artists instead of mainstream acts like Queen, Radiohead, or The Beatles. The canonical data source is `wxyc-shared/src/test-utils/wxyc-example-data.json`.

Preferred defaults for fixtures:
- `ParsedRequest`: `artist="Juana Molina", song="la paradoja", album="DOGA"`
- `LibraryItem`: `artist="Stereolab", title="Aluminum Tunes", genre="Rock"`
- Other good choices: Cat Power / "Moon Pix" (Matador), Jessica Pratt / "On Your Own Love Again" (Drag City), Chuquimamani-Condori / "Edits" (self-released), Duke Ellington & John Coltrane / "Duke Ellington & John Coltrane" (Impulse Records), Sessa / "Pequena Vertigem de Amor" (Mexican Summer), Large Professor / "1st Class" (Matador Records)
