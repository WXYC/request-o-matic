# Request-O-Matic

A FastAPI service that processes song requests for WXYC radio. It parses natural language messages using Groq AI, delegates search to [library-metadata-lookup](https://github.com/WXYC/library-metadata-lookup), and posts enriched results to Slack.

## Features

- **Smart Song Parsing**: Uses Groq AI to extract structured metadata from natural language song requests
- **Library and Discogs Search**: Delegates to [library-metadata-lookup](https://github.com/WXYC/library-metadata-lookup) for library catalog search and Discogs cross-referencing
- **Slack Integration**: Posts enriched song data to Slack with embedded artwork
- **Fast API**: Built with FastAPI for high performance and automatic API documentation

## Prerequisites

- Python 3.12 or higher
- pip (Python package installer) or use the included `pyproject.toml` for modern package management

## Local Setup

### 1. Clone the Repository

```bash
git clone <repository-url>
cd request-o-matic
```

### 2. Create Virtual Environment

```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
```

> [uv](https://docs.astral.sh/uv/) users can skip this step — `uv sync` (below) creates and manages `.venv/` automatically.

### 3. Install Dependencies

Dependencies are pinned via `uv.lock` (the single source of truth); `requirements.txt` (runtime) and `requirements-dev.txt` (runtime + dev tools) are generated from it. See [docs/deployment.md](docs/deployment.md#dependency-management) for the policy and the regenerate/bump procedure.

**Recommended — using [uv](https://docs.astral.sh/uv/)** (installs the exact locked versions into `.venv/`):
```bash
uv sync --extra dev    # omit --extra dev for a runtime-only environment
```

**Using pip** (from the pinned requirements files):
```bash
pip install -r requirements-dev.txt   # or requirements.txt for runtime only
```

> Avoid `pip install -e .` / `pip install -e ".[dev]"` for routine setup — they re-resolve dependencies from PyPI and ignore `uv.lock`, so local versions can silently drift from what CI and Railway run. Bump deliberately via the procedure in [docs/deployment.md](docs/deployment.md#dependency-management).

### 4. Configure Environment Variables

Copy the example environment file and update with your values:

```bash
cp .env.example .env
```

Then edit `.env` with your actual configuration:

```bash
# Required
GROQ_API_KEY=your_groq_api_key_here
LOOKUP_SERVICE_URL=https://library-metadata-lookup-staging.up.railway.app/api/v1

# Optional - Slack Integration (legacy webhook, default transport)
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/YOUR/WEBHOOK/URL

# Optional - Slack Integration (bot-token transport, behind SLACK_USE_BOT_TOKEN)
SLACK_USE_BOT_TOKEN=false
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_CHANNEL_ID=C0123456789

# Optional - Telemetry
POSTHOG_API_KEY=your_posthog_project_api_key
POSTHOG_HOST=https://us.i.posthog.com

# Application Configuration
LOG_LEVEL=INFO
PORT=8000

# Feature Flags
ENABLE_SLACK_INTEGRATION=true
```

#### Getting API Keys

- **GROQ_API_KEY**: Sign up at [Groq](https://console.groq.com/) (not Grok) to get an API key
- **SLACK_WEBHOOK_URL**: Create an incoming webhook in your Slack workspace's [App Settings](https://api.slack.com/messaging/webhooks). Used unless `SLACK_USE_BOT_TOKEN=true`.
- **SLACK_BOT_TOKEN** / **SLACK_CHANNEL_ID**: Only needed when `SLACK_USE_BOT_TOKEN=true`. Install a Slack app with the `chat:write` scope, copy its bot token (`xoxb-...`), and `/invite` the bot into the target channel -- the app does not have `chat:write.public`, so an un-invited channel fails every post with `not_in_channel`.
- **POSTHOG_API_KEY**: Optional - Get your project API key from [PostHog](https://posthog.com/) for telemetry tracking

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

**Note**: All API endpoints (except `/health`) are versioned under `/api/v1/` prefix.

## Docker Setup

### Build and Run with Docker

```bash
# Build the image
docker build -t request-o-matic .

# Run the container
docker run -p 8000:8000 \
  -e GROQ_API_KEY=your_groq_api_key \
  -e LOOKUP_SERVICE_URL=https://library-metadata-lookup-staging.up.railway.app/api/v1 \
  -e SLACK_WEBHOOK_URL=your_slack_webhook \
  request-o-matic
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

All endpoints except `/health` and `/admin/*` are prefixed with `/api/v1`:

- `GET /health` - Health check with service status details (groq, lookup, slack)
- `POST /api/v1/parse` - Parse a natural language song request into structured metadata
- `POST /api/v1/request` - Full request workflow: parse -> delegate search to lookup service -> post to Slack

### Admin Endpoints

Request-line ban management (`Authorization: Bearer $ADMIN_TOKEN`). All writes are proxied to Backend-Service; request-o-matic owns no ban state. Full operator runbook in [`docs/admin-bans.md`](docs/admin-bans.md).

- `POST /admin/bans` - Create or update a ban for a fingerprint (idempotent)
- `DELETE /admin/bans/{fingerprint}` - Remove a ban (idempotent)
- `GET /admin/bans` - List bans (keyset-paginated)

### Example Requests

**Parse a message:**
```bash
curl -X POST "http://localhost:8000/api/v1/parse" \
  -H "Content-Type: application/json" \
  -d '{"message": "Play la paradoja by Juana Molina"}'
```

**Full request workflow:**
```bash
curl -X POST "http://localhost:8000/api/v1/request" \
  -H "Content-Type: application/json" \
  -d '{"message": "Play la paradoja by Juana Molina"}'
```

**Full request workflow (bypass the lookup cache):**
```bash
curl -X POST "http://localhost:8000/api/v1/request" \
  -H "Content-Type: application/json" \
  -d '{"message": "Play la paradoja by Juana Molina", "skip_cache": true}'
```

The `skip_cache` flag is forwarded as `?skip_cache=true` to [library-metadata-lookup](https://github.com/WXYC/library-metadata-lookup), bypassing that service's caches so the lookup resolves against fresh data. Useful for benchmarking and cache A/B comparisons — see `scripts/benchmark_requests.py` and [`docs/benchmark-results.md`](docs/benchmark-results.md).

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

**Run integration tests (real Groq API):**
```bash
pytest tests/integration/ -m external_api
```

**Run performance suite (slow, real Groq API):**
```bash
pytest tests/performance/ -m "external_api and slow"
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

### Slack Issues

If Slack integration fails:
1. With the default webhook transport: verify `SLACK_WEBHOOK_URL` is correct, or that the app can fetch one from Railway if it's unset.
2. With `SLACK_USE_BOT_TOKEN=true`: verify `SLACK_BOT_TOKEN` and `SLACK_CHANNEL_ID` are both set. A `not_in_channel` error means the bot hasn't been `/invite`d into `SLACK_CHANNEL_ID` -- the app has `chat:write` but not `chat:write.public`.
3. Check that your Slack app has proper permissions.

## Environment Variables Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GROQ_API_KEY` | Yes | - | API key for Groq AI service |
| `LOOKUP_SERVICE_URL` | Yes | - | Base URL of library-metadata-lookup service |
| `SLACK_WEBHOOK_URL` | No | - | Slack incoming webhook URL (fetches from Railway if not set); used unless `SLACK_USE_BOT_TOKEN=true` |
| `SLACK_WEBHOOK_KEY_URL` | No | - | Railway endpoint to fetch Slack webhook key |
| `SLACK_USE_BOT_TOKEN` | No | false | Post via `chat.postMessage` with `SLACK_BOT_TOKEN` instead of the incoming webhook |
| `SLACK_BOT_TOKEN` | No | - | Slack bot token (`xoxb-...`); required when `SLACK_USE_BOT_TOKEN=true` |
| `SLACK_CHANNEL_ID` | No | - | Channel ID to post to via `chat.postMessage`; required when `SLACK_USE_BOT_TOKEN=true` |
| `PORT` | No | 8000 | Port for the application to listen on |
| `HOST` | No | 0.0.0.0 | Host to bind the server to |
| `LOG_LEVEL` | No | INFO | Logging level (DEBUG, INFO, WARNING, ERROR) |
| `ENABLE_SLACK_INTEGRATION` | No | true | Enable/disable Slack notifications |
| `ENABLE_TELEMETRY` | No | true | Enable/disable PostHog telemetry |
| `POSTHOG_API_KEY` | No | - | PostHog project API key for telemetry tracking |
| `POSTHOG_HOST` | No | https://us.i.posthog.com | PostHog host URL |
| `SENTRY_DSN` | No | - | Sentry DSN for error tracking |
| `ADMIN_TOKEN` | No | - | Bearer token gating `/admin/bans`. Fail-closed when unset. |
| `BS_INTERNAL_BANS_URL` | No | - | Base URL of Backend-Service's `/internal/banned-fingerprints` CRUD (BS#1261). |
| `BS_INTERNAL_KEY` | No | - | Shared secret forwarded as `X-Internal-Key` on calls to BS internal endpoints. |

## Architecture

### Key Design Decisions

1. **Service Delegation**: All library search and Discogs cross-referencing is delegated to [library-metadata-lookup](https://github.com/WXYC/library-metadata-lookup) via HTTP
2. **Dependency Injection**: FastAPI's dependency injection system manages service lifecycle and makes testing easier
3. **Centralized Configuration**: Pydantic Settings for type-safe, validated configuration
4. **Async Throughout**: All I/O operations use async/await for optimal performance
5. **Custom Exceptions**: Domain-specific exceptions for better error handling and debugging
6. **Error Tracking**: Sentry integration for production error monitoring with breadcrumbs for debugging

### Service Lifecycle

Services are managed through FastAPI's lifespan context manager:
- HTTP clients are reused across requests
- Resources are properly cleaned up at shutdown

## Deployment

- Hosted on Railway
- `main` branch auto-deploys to **staging**
- `prod` branch auto-deploys to **production**
