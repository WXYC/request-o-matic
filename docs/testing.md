# Testing

## Test Types

| Type | Location | Marker(s) | External Services | Purpose |
|------|----------|-----------|-------------------|---------|
| Unit | `tests/unit/` | (none) | Mocked | Fast, isolated component tests |
| Integration | `tests/integration/` | `external_api` | Real Groq API | End-to-end verification |
| Performance | `tests/performance/` | `external_api`, `slow` | Real Groq API | Response time benchmarks |

## Marker scheme

Markers follow the canonical "architecture A" vocabulary defined in [the WXYC wiki test-patterns doc](https://github.com/WXYC/wiki/blob/main/plans/test-patterns.md), Section 3. Markers route CI by infrastructure; tier (unit / integration / performance) is documented by directory layout. The set of markers used by this repo:

- **`external_api`** — needs network egress and real third-party API keys (Groq, Slack). Default `pytest` deselects them; opt in with `-m "external_api"`.
- **`slow`** — orthogonal cost dimension, takes more than ~10s. Used together with `external_api` on the performance suite. Opted out from the marker-sync check (`# ci-sync-skip: slow ...` in `pyproject.toml`) because the performance suite is run manually against staging/production with `TEST_ENV` set.
- **`contract`** — per-repo addition (legitimate per Section 3, "What is NOT in the marker namespace"). Reserved for tests that verify the *shape* of an external API contract (Slack, Groq) rather than just calling it. Currently no tests use it; the marker is declared so that future contract tests have a stable name. Opted out from the marker-sync check because, by design, it is manual-only.

The reusable check at `WXYC/wxyc-etl/.github/workflows/check-ci-marker-sync.yml` is wired into `ci.yml` as the `marker-sync` job. It guards the invariant that every marker actually used by a test is either selected by some CI invocation or explicitly opted out.

## Unit Tests
Use mocks for all external services (Groq, lookup service). Run frequently during development:
```bash
venv/bin/python -m pytest tests/unit/ -v
```

## External-API Tests
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

## Test Environment Configuration
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

## Local Server Testing
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

## Bug Fix Protocol
**For every request bug where a lookup fails to find the correct release:**
1. Create a **unit test** in `tests/unit/` that reproduces the bug with mocked data
2. Create an **integration test** in `tests/integration/` (`@pytest.mark.external_api`) that verifies the fix against real APIs
3. The integration test should assert that false positives are excluded AND correct results are included
