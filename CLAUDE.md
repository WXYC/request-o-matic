# Claude Code Instructions for Request-O-Matic

## Project Overview

Request-O-Matic is a FastAPI service for WXYC radio that processes song requests. It parses natural language messages, searches a local library catalog, fetches album artwork, and posts enriched results to Slack.

## Architecture

### Request Flow
1. **Parse**: Groq AI (`llama-3.1-8b-instant`) extracts artist/song/album from message
2. **Album Lookup**: If song provided without album, query Discogs for album name
3. **Library Search**: Search SQLite database with fuzzy matching
4. **Artist Filtering**: Filter results to match requested artist (prefix matching)
5. **Compilation Search**: If no results, search for track on compilations
6. **Track Validation**: If fallback returned all artist albums, validate each against Discogs tracklists to keep only albums containing the requested track
7. **Artwork**: Fetch album art from Discogs
8. **Slack**: Post formatted results with artwork

### Key Files
- `routers/request.py` - Main request handling and search orchestration
- `services/parser.py` - Groq AI message parsing
- `library/db.py` - SQLite full-text search with FTS5 and fuzzy fallback
- `discogs/service.py` - Discogs API service with optional PostgreSQL cache
- `discogs/cache_service.py` - PostgreSQL cache for Discogs data (reduces API calls)
- `discogs/memory_cache.py` - In-memory TTL cache for API responses
- `services/lookup_client.py` - HTTP client for library-metadata-lookup delegation
- `core/sentry.py` - Sentry error tracking integration
- `core/telemetry.py` - PostHog telemetry with cache stats tracking
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
The cache ETL pipeline lives in a separate repo: [WXYC/discogs-cache](https://github.com/WXYC/discogs-cache). See that repo for setup instructions. The SQL schema files in `discogs-cache/schema/` define the shared contract between the ETL pipeline and this service's `cache_service.py`.

### Library ETL
The `library.db` SQLite database is synced daily from the WXYC MySQL database:

- **`scripts/sync-library.sh`** - Orchestrates ETL, commits changes, and pushes to staging
- **`scripts/export_to_sqlite.py`** - Connects via SSH to remote MySQL, exports to SQLite with FTS5

The sync runs daily at 7 AM via launchd (`~/Library/LaunchAgents/com.wxyc.request-parser-etl.plist`).

**Manual sync:**
```bash
# Run ETL (no Slack notifications)
./scripts/sync-library.sh

# Run with Slack error notifications
./scripts/sync-library.sh --notify
```

**Logs:** `~/Library/Logs/request-parser-etl.log`

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
Use mocks for all external services (Groq, Discogs, database). Run frequently during development:
```bash
venv/bin/python -m pytest tests/unit/ -v
```

### Integration Tests
Hit real Discogs/Groq APIs using staging environment variables from Railway. The `conftest.py` automatically loads staging env vars when the Railway CLI is available:
```bash
# Requires RAILWAY_TOKEN_STAGING env var or Railway CLI login
venv/bin/python -m pytest tests/integration/ -v -m integration

# Or use the helper script
./test-integration.sh -v
```

Integration tests are skipped if required env vars (`DISCOGS_TOKEN`, `GROQ_API_KEY`) are missing.

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
- **`scripts/lookup.py`** - One-off lookups against production (default) or local (`--local`). Shows Discogs URLs for each library result.
- **`scripts/repl.py`** - Interactive REPL with command history, server switching (`:local`/`:prod`)
- **`scripts/create_posthog_dashboard.py`** - Creates PostHog dashboard for telemetry visualization (requires `POSTHOG_PERSONAL_API_KEY` and `POSTHOG_PROJECT_ID`)

### Bug Fix Protocol
**For every request bug where a lookup fails to find the correct release:**
1. Create a **unit test** in `tests/unit/` that reproduces the bug with mocked data
2. Create an **integration test** in `tests/integration/` that verifies the fix against real APIs
3. The integration test should assert that false positives are excluded AND correct results are included

Example (from Sugar Plant bug fix):
- Unit test: `test_search_releases_filters_invalid_compilations` - mocks Discogs responses
- Integration test: `test_sugar_plant_excludes_unrelated_compilations` - hits real API

## Deployment

- Hosted on Railway
- `main` branch auto-deploys to **staging**
- `prod` branch auto-deploys to **production**
- Use `railway` CLI for status/logs (requires TTY for some commands)

## Common Issues and Fixes

### False Positive Artist Matches
The `filter_results_by_artist()` function uses **prefix matching** to avoid:
- "Toy" matching "Chew Toy"
- "Young Gov" matching "Young Black Teenagers"

Artists must appear at the START of the result's artist field.

### Ambiguous "X - Y" Formats
Messages like "Artist - Title" or "Title - Artist" are ambiguous. The `detect_ambiguous_format()` and `search_with_alternative_interpretation()` functions try both interpretations and return all matches.

### Compilation Search False Positives
The keyword search in `search_compilations_for_track()` filters results by artist to prevent matching albums that happen to share a song title (e.g., "The All Seeing Eye" album by Wayne Shorter when searching for a song by Toy).

## Environment Variables

Required:
- `GROQ_API_KEY` - For AI parsing

Optional:
- `DISCOGS_TOKEN` - For artwork and track lookup
- `SLACK_WEBHOOK_URL` - For posting results
- `SENTRY_DSN` - For error tracking (Sentry)
- `DATABASE_URL_DISCOGS` - PostgreSQL URL for Discogs cache (e.g., `postgresql://user:pass@host:5432/discogs`)
- `LOOKUP_SERVICE_URL` - Base URL of library-metadata-lookup service (e.g., `https://library-metadata-lookup-staging.up.railway.app/api/v1`). When set, delegates all search operations to this service. If unset, uses inline search pipeline. Errors propagate as 502.

Library ETL (for `scripts/sync-library.sh`):
- `LIBRARY_SSH_HOST` - SSH host to connect to
- `LIBRARY_SSH_USER` - SSH username
- `LIBRARY_DB_HOST` - MySQL host (as seen from SSH host)
- `LIBRARY_DB_USER` - MySQL username
- `LIBRARY_DB_PASSWORD` - MySQL password
- `LIBRARY_DB_NAME` - MySQL database name
- `SLACK_MONITORING_WEBHOOK` - Webhook for error notifications (used with `--notify`)

## Code Style

- Line length: 100 chars
- Use `black` for formatting, `ruff` for linting
- Type hints encouraged but not enforced
- Async/await for all I/O operations
