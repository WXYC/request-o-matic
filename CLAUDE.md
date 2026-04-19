# Claude Code Instructions for Request-O-Matic

## Project Overview

Request-O-Matic is a FastAPI service for WXYC radio that processes song requests. It parses natural language messages, delegates search to [library-metadata-lookup](https://github.com/WXYC/library-metadata-lookup), and posts enriched results to Slack.

## Architecture

### Request Flow
1. **Parse**: Groq AI (`llama-3.1-8b-instant`) extracts artist/song/album from message
2. **Early return**: Non-request messages (feedback, DJ messages) are posted to Slack without search
3. **Delegate**: Search is delegated to [library-metadata-lookup](https://github.com/WXYC/library-metadata-lookup) via HTTP (`LOOKUP_SERVICE_URL`). If not configured, returns HTTP 503.
4. **Slack**: Post enriched results with artwork to Slack

### Key Files
- `models.py` - Shared DTOs: `LibraryItem`, `ReleaseMetadata`
- `routers/request.py` - Request handling: parse, delegate to lookup service, post to Slack
- `routers/health.py` - Health check endpoint (groq, lookup, slack services)
- `services/parser.py` - Groq AI message parsing
- `services/lookup_client.py` - HTTP client for library-metadata-lookup delegation
- `services/slack.py` - Slack message formatting and posting
- `core/dependencies.py` - FastAPI dependency injection (HTTP client, Groq, Slack, PostHog)
- `core/sentry.py` - Sentry error tracking integration
- `core/telemetry.py` - PostHog telemetry with cache stats tracking
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
The `library.db` SQLite database is synced daily from the WXYC MySQL database by the ETL pipeline in [WXYC/discogs-etl](https://github.com/WXYC/discogs-etl). The pipeline builds `library.db` via `wxyc-export-to-sqlite` (from [WXYC/wxyc-catalog](https://github.com/WXYC/wxyc-catalog)), enriches it with streaming links from `streaming_availability.db` (in library-metadata-lookup), and uploads the result to the LML staging and production services via `POST /admin/upload-library-db`. See `discogs-etl/scripts/sync-library.sh` and `discogs-etl/.github/workflows/sync-library.yml` for details.

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

| Type | Location | External Services | Purpose |
|------|----------|-------------------|---------|
| Unit | `tests/unit/` | Mocked | Fast, isolated component tests |
| Integration | `tests/integration/` | Real APIs | End-to-end verification |
| Performance | `tests/performance/` | Real APIs | Response time benchmarks |

### Unit Tests
Use mocks for all external services (Groq, lookup service). Run frequently during development:
```bash
venv/bin/python -m pytest tests/unit/ -v
```

### Integration Tests
Hit real Groq API using staging environment variables from Railway. The `conftest.py` automatically loads staging env vars when the Railway CLI is available:
```bash
# Requires RAILWAY_TOKEN_STAGING env var or Railway CLI login
venv/bin/python -m pytest tests/integration/ -v -m integration

# Or use the helper script
./test-integration.sh -v
```

Integration tests are skipped if required env vars (`GROQ_API_KEY`) are missing.

### Test Environment Configuration
Use `TEST_ENV` to control which server integration and performance tests hit:

```bash
# Test against local server (default) - requires running uvicorn locally
TEST_ENV=local venv/bin/python -m pytest tests/integration/ -v -m integration

# Test against staging server on Railway
TEST_ENV=staging venv/bin/python -m pytest tests/integration/ -v -m integration

# Test against production server on Railway
TEST_ENV=production venv/bin/python -m pytest tests/integration/ -v -m integration
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

**Note:** Integration and performance tests with `TEST_ENV=local` automatically start and stop the local server, so you don't need to manually run uvicorn first. The test fixture detects if a server is already running and skips startup if so.

### Manual Testing Tools
- **`scripts/lookup.py`** - One-off lookups against production (default) or local (`--local`).
- **`scripts/repl.py`** - Interactive REPL with command history, server switching (`:local`/`:prod`)
- **`scripts/create_posthog_dashboard.py`** - Creates PostHog dashboard for telemetry visualization (requires `POSTHOG_PERSONAL_API_KEY` and `POSTHOG_PROJECT_ID`)

### Bug Fix Protocol
**For every request bug where a lookup fails to find the correct release:**
1. Create a **unit test** in `tests/unit/` that reproduces the bug with mocked data
2. Create an **integration test** in `tests/integration/` that verifies the fix against real APIs
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
- `LOOKUP_SERVICE_URL` - Base URL of library-metadata-lookup service (e.g., `https://library-metadata-lookup-staging.up.railway.app/api/v1`). All search is delegated to this service. If unset, song requests return HTTP 503.

Optional:
- `SLACK_WEBHOOK_URL` - For posting results
- `SLACK_WEBHOOK_KEY_URL` - Railway endpoint to fetch Slack webhook key
- `SENTRY_DSN` - For error tracking (Sentry)
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
