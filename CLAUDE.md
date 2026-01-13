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

## Testing

### Unit Tests
Tests in `tests/unit/` use mocks and don't hit real services:
```bash
venv/bin/python -m pytest tests/unit/ -v
```

### Testing Against Production
Use the lookup script to test without posting to Slack:
```bash
venv/bin/python scripts/lookup.py "song request message here"
```

The script calls the deployed API at `https://request-o-matic-production.up.railway.app/api/v1/request` with `skip_slack=true`.

## Deployment

- Hosted on Railway
- Auto-deploys from `prod` branch
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

### Branches
- `main` is for staging
- `prod` is for production

### Testing Against Staging
- When fixing lookup bugs, use integration tests to validate the fix. You can run the server locally to do this.

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
