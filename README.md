# Request-O-Matic

A FastAPI service that supplements song requests with structured metadata, album artwork, and library catalog information. Built to enhance music request workflows with automated data enrichment and Slack integration.

## Features

- **Smart Song Parsing**: Uses Groq AI to extract structured metadata from natural language song requests
- **Album Artwork Lookup**: Fetches album artwork from Discogs
- **Library Catalog Search**: Full-text search across a local SQLite music library database
- **Slack Integration**: Posts enriched song data to Slack with embedded artwork
- **Fast API**: Built with FastAPI for high performance and automatic API documentation

## Prerequisites

- Python 3.12 or higher
- pip (Python package installer) or use the included `pyproject.toml` for modern package management

## Local Setup

### 1. Clone the Repository

```bash
git clone <repository-url>
cd request-parser
```

### 2. Create Virtual Environment (Recommended)

```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

### 3. Install Dependencies

**Option A: Using requirements.txt**
```bash
pip install -r requirements.txt
```

**Option B: Using pyproject.toml (Recommended)**
```bash
pip install -e .
```

**Option C: With development dependencies**
```bash
pip install -e ".[dev]"
```

### 4. Configure Environment Variables

Copy the example environment file and update with your values:

```bash
cp .env.example .env
```

Then edit `.env` with your actual configuration:

```bash
# Required
GROQ_API_KEY=your_groq_api_key_here

# Optional - Artwork Lookup
DISCOGS_TOKEN=your_discogs_token_here

# Optional - Slack Integration
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/YOUR/WEBHOOK/URL

# Optional - Telemetry
POSTHOG_API_KEY=your_posthog_project_api_key
POSTHOG_HOST=https://us.i.posthog.com

# Application Configuration
LOG_LEVEL=INFO
PORT=8000

# Feature Flags
ENABLE_SLACK_INTEGRATION=true
ENABLE_ARTWORK_LOOKUP=true
```

#### Getting API Keys

- **GROQ_API_KEY**: Sign up at [Groq](https://console.groq.com/) (not Grok) to get an API key
- **DISCOGS_TOKEN**: Create a personal access token at [Discogs Settings](https://www.discogs.com/settings/developers)
- **SLACK_WEBHOOK_URL**: Create an incoming webhook in your Slack workspace's [App Settings](https://api.slack.com/messaging/webhooks)
- **POSTHOG_API_KEY**: Optional - Get your project API key from [PostHog](https://posthog.com/) for telemetry tracking

### 4. Verify Database

The project includes a pre-built SQLite database (`library.db`). If you need to rebuild it or if the file is missing:

```bash
python scripts/export_to_sqlite.py
```

### 5. Run the Application

#### Option A: Using Python directly

```bash
python main.py
```

#### Option B: Using uvicorn

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

The `--reload` flag enables auto-reloading during development.

The application will start on `http://localhost:8000`

### 6. Access the API

- **Interactive API Documentation**: http://localhost:8000/docs (Swagger UI - Try out endpoints here!)
- **Read-Only Docs**: http://localhost:8000/redoc (ReDoc - Beautiful documentation)
- **Health Check**: http://localhost:8000/health (Detailed service status)

**Note**: All API endpoints (except `/health`) are now versioned under `/api/v1/` prefix.

## Docker Setup

### Build and Run with Docker

```bash
# Build the image
docker build -t request-parser .

# Run the container
docker run -p 8000:8000 \
  -e GROQ_API_KEY=your_groq_api_key \
  -e DISCOGS_TOKEN=your_discogs_token \
  -e SLACK_WEBHOOK_URL=your_slack_webhook \
  request-parser
```

### Using Docker Compose

Create a `docker-compose.yml`:

```yaml
version: '3.8'

services:
  app:
    build: .
    ports:
      - "8000:8000"
    environment:
      - GROQ_API_KEY=${GROQ_API_KEY}
      - DISCOGS_TOKEN=${DISCOGS_TOKEN}
      - SLACK_WEBHOOK_URL=${SLACK_WEBHOOK_URL}
    env_file:
      - .env
```

Then run:

```bash
docker-compose up
```

## API Endpoints

### Core Endpoints (v1)

All endpoints except `/health` are prefixed with `/api/v1`:

- `GET /health` - Health check with service status details
- `POST /api/v1/parse` - Parse a natural language song request into structured metadata
- `POST /api/v1/request` - Full request workflow: parse → search library → find artwork → post to Slack
- `POST /api/v1/artwork` - Find album artwork for a given song/album/artist
- `GET /api/v1/library/search` - Search the library catalog

### Example Requests

**Parse a message:**
```bash
curl -X POST "http://localhost:8000/api/v1/parse" \
  -H "Content-Type: application/json" \
  -d '{"message": "Play Bohemian Rhapsody by Queen"}'
```

**Full request workflow:**
```bash
curl -X POST "http://localhost:8000/api/v1/request" \
  -H "Content-Type: application/json" \
  -d '{"message": "Play Abele Dance (85 remix) by Manu Dibango"}'
```

**Search library:**
```bash
curl "http://localhost:8000/api/v1/library/search?q=Queen+Bohemian&limit=5"
```

**Health check:**
```bash
curl "http://localhost:8000/health"
```

## Development

### Running Tests

**Run all tests (excluding integration):**
```bash
pytest
```

**Run unit tests only:**
```bash
pytest tests/unit/
```

**Run integration tests:**
```bash
pytest tests/integration/ -m integration
```

**Run with coverage:**
```bash
pytest --cov=. --cov-report=html
```

### Code Quality

The project is configured with modern Python tooling via `pyproject.toml`:

**Format code:**
```bash
black .
```

**Lint code:**
```bash
ruff check .
```

**Fix linting issues automatically:**
```bash
ruff check --fix .
```

**Type checking:**
```bash
mypy .
```

**Run all quality checks:**
```bash
black . && ruff check --fix . && mypy . && pytest
```

### Development Workflow

1. Create a feature branch
2. Make your changes
3. Run tests and linters
4. Submit a pull request

The project uses:
- **Pydantic Settings** for type-safe configuration
- **FastAPI dependency injection** for clean architecture
- **Async/await** throughout for performance
- **Comprehensive logging** with structured output
- **Custom exceptions** for better error handling

## Troubleshooting

### Database Not Found Error

If you see an error about `library.db` not being found:

```bash
python scripts/export_to_sqlite.py
```

### GROQ_API_KEY Not Set Error

Ensure your `.env` file exists in the project root and contains:

```
GROQ_API_KEY=your_actual_key_here
```

### Port Already in Use

If port 8000 is already in use, specify a different port:

```bash
uvicorn main:app --port 8001
```

### Slack Webhook Issues

If Slack integration fails:
1. Verify your webhook URL is correct
2. Check that your Slack app has proper permissions
3. The app will attempt to fetch a webhook from Railway if `SLACK_WEBHOOK_URL` is not set

## Environment Variables Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GROQ_API_KEY` | Yes | - | API key for Groq AI service |
| `DISCOGS_TOKEN` | No | - | Personal access token for Discogs API |
| `SLACK_WEBHOOK_URL` | No | - | Slack incoming webhook URL (fetches from Railway if not set) |
| `PORT` | No | 8000 | Port for the application to listen on |
| `HOST` | No | 0.0.0.0 | Host to bind the server to |
| `LOG_LEVEL` | No | INFO | Logging level (DEBUG, INFO, WARNING, ERROR) |
| `LIBRARY_DB_PATH` | No | library.db | Path to SQLite library database |
| `ENABLE_SLACK_INTEGRATION` | No | true | Enable/disable Slack notifications |
| `ENABLE_ARTWORK_LOOKUP` | No | true | Enable/disable artwork lookup from external APIs |
| `POSTHOG_API_KEY` | No | - | PostHog project API key for telemetry tracking |
| `POSTHOG_HOST` | No | https://us.i.posthog.com | PostHog host URL |
| `SENTRY_DSN` | No | - | Sentry DSN for error tracking |
| `DATABASE_URL_DISCOGS` | No | - | PostgreSQL URL for Discogs cache (see [Discogs Cache Setup](#discogs-cache-setup)) |

## Discogs Cache Setup

The service supports an optional PostgreSQL cache for Discogs data to reduce API calls. When enabled, the service queries the local cache first and falls back to the Discogs API on cache misses.

The cache ETL pipeline (building the database from Discogs data dumps) lives in a separate repo: [WXYC/discogs-cache](https://github.com/WXYC/discogs-cache). See that repo for full setup instructions.

To enable the cache, set the environment variable:

```bash
DATABASE_URL_DISCOGS=postgresql://user:pass@host:5432/discogs
```

If `DATABASE_URL_DISCOGS` is not set, the service uses the Discogs API directly (existing behavior).

## Architecture

### Key Design Decisions

1. **Dependency Injection**: FastAPI's dependency injection system manages service lifecycle and makes testing easier
2. **Centralized Configuration**: Pydantic Settings for type-safe, validated configuration
3. **Modular Structure**: Each feature (artwork, library, parsing) is self-contained
4. **Async Throughout**: All I/O operations use async/await for optimal performance
5. **Custom Exceptions**: Domain-specific exceptions for better error handling and debugging
6. **Comprehensive Logging**: Structured logging at appropriate levels throughout the application
7. **Hybrid Caching**: Optional PostgreSQL cache built from Discogs data dumps with trigram fuzzy matching, graceful degradation to API-only mode
8. **Error Tracking**: Sentry integration for production error monitoring with breadcrumbs for debugging

### Service Lifecycle

Services are managed through FastAPI's lifespan context manager:
- Database connections are established at startup
- HTTP clients are reused across requests
- Resources are properly cleaned up at shutdown

## License

[Add your license here]

## Contributing

[Add contribution guidelines here]

## Support

For issues and questions, please [open an issue](https://github.com/your-repo/request-parser/issues) on GitHub.
