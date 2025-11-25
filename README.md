# Request-O-Matic

A FastAPI service that supplements song requests with structured metadata, album artwork, and library catalog information. Built to enhance music request workflows with automated data enrichment and Slack integration.

## Features

- 🎵 **Smart Song Parsing**: Uses Groq AI to extract structured metadata from natural language song requests
- 🎨 **Album Artwork Lookup**: Fetches album artwork from Discogs
- 📚 **Library Catalog Search**: Full-text search across a local SQLite music library database
- 💬 **Slack Integration**: Posts enriched song data to Slack with embedded artwork
- ⚡ **Fast API**: Built with FastAPI for high performance and automatic API documentation

## Prerequisites

- Python 3.12 or higher
- pip (Python package installer)

## Local Setup

### 1. Clone the Repository

```bash
git clone <repository-url>
cd request-parser
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure Environment Variables

Create a `.env` file in the project root:

```bash
# Required
GROQ_API_KEY=your_groq_api_key_here

# Optional - for artwork lookup
DISCOGS_TOKEN=your_discogs_token_here

# Optional - for Slack integration
# If not provided, will attempt to fetch from Railway endpoint
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/YOUR/WEBHOOK/URL
```

#### Getting API Keys

- **GROQ_API_KEY**: Sign up at [Groq](https://console.groq.com/) to get an API key
- **DISCOGS_TOKEN**: Create a personal access token at [Discogs Settings](https://www.discogs.com/settings/developers)
- **SLACK_WEBHOOK_URL**: Create an incoming webhook in your Slack workspace's [App Settings](https://api.slack.com/messaging/webhooks)

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
- **Health Check**: http://localhost:8000/health (Simple status endpoint)

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

### Core Endpoints

- `GET /health` - Health check endpoint
- `POST /parse` - Parse a natural language song request into structured metadata
- `POST /request` - Full request workflow: parse → search library → find artwork → post to Slack
- `POST /artwork` - Find album artwork for a given song/album/artist
- `GET /library/search` - Search the library catalog

### Example Request

```bash
curl -X POST "http://localhost:8000/parse" \
  -H "Content-Type: application/json" \
  -d '{"text": "Play Bohemian Rhapsody by Queen"}'
```

## Project Structure

```
request-parser/
├── main.py              # Application entry point
├── requirements.txt     # Python dependencies
├── library.db          # SQLite music library database
├── .env                # Environment variables (create this)
├── artwork/            # Artwork lookup module
│   ├── finder.py       # Artwork search orchestration
│   ├── models.py       # Data models
│   ├── router.py       # API routes
│   └── providers/      # Provider implementations
│       ├── base.py     # Base provider interface
│       └── discogs.py  # Discogs API integration
├── library/            # Library catalog module
│   ├── db.py          # SQLite database client
│   ├── models.py      # Data models
│   └── router.py      # API routes
├── routers/           # API route handlers
│   ├── health.py     # Health check
│   ├── parse.py      # Text parsing
│   └── request.py    # Main request workflow
├── services/         # Core services
│   ├── groq.py      # Groq AI client
│   ├── parser.py    # Song request parser
│   └── slack.py     # Slack integration
├── scripts/         # Utility scripts
│   └── export_to_sqlite.py  # Database export tool
└── tests/           # Test suite
    └── test_artwork.py
```

## Development

### Running Tests

```bash
pytest
```

### Running Tests with Coverage

```bash
pytest --cov=. --cov-report=html
```

### Code Quality

The project uses standard Python tooling:

```bash
# Type checking
mypy .

# Linting
flake8 .

# Formatting
black .
```

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
| `SLACK_WEBHOOK_URL` | No | - | Slack incoming webhook URL |
| `PORT` | No | 8000 | Port for the application to listen on |

## License

[Add your license here]

## Contributing

[Add contribution guidelines here]

## Support

For issues and questions, please [open an issue](https://github.com/your-repo/request-parser/issues) on GitHub.

