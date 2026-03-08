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
LOOKUP_SERVICE_URL=https://library-metadata-lookup-staging.up.railway.app/api/v1

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
```

#### Getting API Keys

- **GROQ_API_KEY**: Sign up at [Groq](https://console.groq.com/) (not Grok) to get an API key
- **SLACK_WEBHOOK_URL**: Create an incoming webhook in your Slack workspace's [App Settings](https://api.slack.com/messaging/webhooks)
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

## API Endpoints

### Core Endpoints (v1)

All endpoints except `/health` are prefixed with `/api/v1`:

- `GET /health` - Health check with service status details (groq, lookup, slack)
- `POST /api/v1/parse` - Parse a natural language song request into structured metadata
- `POST /api/v1/request` - Full request workflow: parse -> delegate search to lookup service -> post to Slack

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
| `LOOKUP_SERVICE_URL` | Yes | - | Base URL of library-metadata-lookup service |
| `SLACK_WEBHOOK_URL` | No | - | Slack incoming webhook URL (fetches from Railway if not set) |
| `SLACK_WEBHOOK_KEY_URL` | No | - | Railway endpoint to fetch Slack webhook key |
| `PORT` | No | 8000 | Port for the application to listen on |
| `HOST` | No | 0.0.0.0 | Host to bind the server to |
| `LOG_LEVEL` | No | INFO | Logging level (DEBUG, INFO, WARNING, ERROR) |
| `ENABLE_SLACK_INTEGRATION` | No | true | Enable/disable Slack notifications |
| `ENABLE_TELEMETRY` | No | true | Enable/disable PostHog telemetry |
| `POSTHOG_API_KEY` | No | - | PostHog project API key for telemetry tracking |
| `POSTHOG_HOST` | No | https://us.i.posthog.com | PostHog host URL |
| `SENTRY_DSN` | No | - | Sentry DSN for error tracking |

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
