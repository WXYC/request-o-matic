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
6. **Artwork**: Fetch album art from Discogs
7. **Slack**: Post formatted results with artwork

### Key Files
- `routers/request.py` - Main request handling and search orchestration
- `services/parser.py` - Groq AI message parsing
- `library/db.py` - SQLite full-text search with FTS5 and fuzzy fallback
- `artwork/providers/discogs.py` - Discogs API integration

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

### Manual Testing Tools
- **`scripts/lookup.py`** - One-off lookups against production (default) or local (`--local`)
- **`scripts/repl.py`** - Interactive REPL with command history, server switching (`:local`/`:prod`)

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

## Code Style

- Line length: 100 chars
- Use `black` for formatting, `ruff` for linting
- Type hints encouraged but not enforced
- Async/await for all I/O operations
