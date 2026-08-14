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

These tests are **not** part of the auto-CI run on `main`. The suite shares a per-org Groq TPM bucket with the staging container and saturates the 6000 TPM cap on `llama-3.1-8b-instant`, so running it on every push cascades into 429s + timeouts (see [#118](https://github.com/WXYC/request-o-matic/issues/118) for the diagnosis). Trigger it manually from the GitHub Actions UI via the **External API Tests** workflow (`.github/workflows/external-api.yml`); it always targets staging. The `deploy-staging` smoke test in `ci.yml` remains the actual deploy gate. The parser half of the suite is also covered automatically — see [Nightly NLP Check](#nightly-nlp-check-conditional) below.

Two caveats when running the manual workflow: (1) trigger it *after* the `deploy-staging` job finishes if you want the test code and the deployed code to match — the suite hits a fixed staging URL, so it sees whatever was last deployed there, regardless of when you trigger. (2) Pick the same git ref in the workflow-dispatch UI as is currently deployed; the test source comes from the chosen ref while the deployed service comes from `main`. Production is deliberately not an option: `TestFullRequestIntegration` POSTs to `/request`, which posts to Slack from the deployed container, so a prod run would spam the live WXYC channel. To test prod, run pytest locally with `TEST_ENV=production` after disabling Slack posting.

## Nightly NLP Check (conditional)

`.github/workflows/nlp-nightly.yml` runs `TestParserIntegration` — the 21 tests that call `parse_request()` against the real Groq model — at **03:00 ET**, but only on nights when the NLP surface actually changed. A week with no parser work costs nothing; a prompt edit is validated against the live model within a day of landing.

The gate is `scripts/nlp_nightly_gate.py`, and it makes two decisions:

1. **Which cron entry is tonight's.** GitHub cron is UTC-only, so 03:00 ET needs two entries — `0 7 * * *` (EDT) and `0 8 * * *` (EST) — and both fire year-round. The gate keeps whichever matches the UTC offset in effect and no-ops the other. It compares offsets rather than wall-clock hours, so GitHub's habitual scheduler lag can delay a run but cannot skip a night.
2. **Whether anything relevant changed.** It diffs `HEAD` against the last commit whose parser suite actually passed, and filters that diff through `WATCHED_PATHS`:

   | Watched | Why |
   |---|---|
   | `services/parser.py` | The prompt, the model pin, and `parse_request()` itself |
   | `routers/parse.py` | The `/parse` entry point |
   | `core/dependencies.py`, `core/groq_tracing.py` | Groq client construction and span wiring |
   | `config/settings.py` | Groq settings/env surface |
   | `tests/scenarios.py` | The shared assertion corpus |
   | `tests/integration/test_integration.py` | The suite itself |
   | `tests/conftest.py`, `tests/integration/conftest.py` | The fixtures the job leans on (`TEST_ENV`, autouse `local_server`) |
   | `core/dependencies.py`, `config/settings.py` | **Only when the diff mentions `groq`** — see below |
   | `.github/workflows/nlp-nightly.yml`, `scripts/nlp_nightly_gate.py` | A broken trigger gets caught by the run it triggers |

The baseline comes from a marker artifact (`nlp-green-sha`, 90-day retention) that only a **passing** suite uploads; the gate reads the commit from the artifact's `workflow_run.head_sha` metadata. It is deliberately not "the head SHA of the last successful workflow run", because both cron entries fire every night and the one that is not tonight's exits green having validated nothing. A run-conclusion baseline would let that decoy advance the baseline to `HEAD` — and under EST the decoy fires an *hour before* the real entry, so the real run would diff `HEAD` against `HEAD` and skip. The suite would never run between November and March, with every job green while it happened.

Two files are watched by keyword rather than wholesale (`KEYWORD_WATCHED`). `core/dependencies.py` also builds the LML, PostHog, and ban clients, and `config/settings.py` carries Slack/LML/PostHog/ban config; over the 120 days before this was written they changed on 15 days, of which exactly one touched a Groq line. Watching them whole would have spent the shared TPM bucket on ban-roster and LML work about half the nights the gate fired. They now trigger only when the diff's own `+`/`-` lines mention `groq`.

`routers/parse.py` is deliberately *not* watched. It looks like NLP surface, but `TestParserIntegration` calls `parse_request()` directly and never routes through `/parse`, so a live run could not validate a change there — `tests/unit/test_parse_router.py` covers it instead.

Because only a passing suite writes the marker, a red night is sticky: the baseline does not advance past a failure, so the suite keeps re-running nightly until it passes. A gated-off night writes nothing either, which keeps the diff anchored to the last commit whose parser behaviour was actually observed. Dependency pins (`requirements*.txt`, `uv.lock`) are deliberately **not** watched — every unrelated dependency bump moves them. After a `groq` SDK bump, trigger the workflow by hand.

Scope is `TestParserIntegration` only. It exercises the prompt and model in-process; `TestFullRequestIntegration` POSTs to a deployed container, which posts to Slack, and stays manual in `external-api.yml`. A missing `GROQ_API_KEY` fails the job rather than skipping into a false green, and a failed run posts to `SLACK_MONITORING_WEBHOOK` the same way `ci.yml` does.

To force a run regardless of the diff, dispatch it manually (**Actions → Nightly NLP Check → Run workflow**, or `gh workflow run nlp-nightly.yml`). To preview the decision locally:

```bash
python scripts/nlp_nightly_gate.py --event-name schedule --event-schedule "0 7 * * *" --base <sha> --head HEAD
```

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

### Prompt-only parser fixes
Some parser bugs are fixed purely by editing `SYSTEM_PROMPT` in `services/parser.py` (no behavior code changes) — e.g. teaching Groq a new phrasing shape. A mocked-Groq unit test is **vacuous** for these: the mock returns whatever JSON it is handed and exercises none of the prompt. For prompt-only fixes the unit half of the protocol is satisfied by a **prompt-contract** test (see `tests/unit/test_parser_prompt_contract.py`) that asserts `SYSTEM_PROMPT` still encodes the rule, so it can't be silently deleted in a refactor. Assert at the *concept* level (the distinguishing phrase the rule adds), not by pinning a literal example sentence — otherwise a benign reword breaks CI without any behavior change. The behavioral verification is the `external_api` integration test; since the fix is prompt-only and Groq is nondeterministic at temp 0.1, write it like the other parser tests there — a single representative run with substring matching — not an exact-equality / require-every-run loop. (The require-every-run loop fits only deterministic fixes like the regex album pre-pass, where N/N passes is a real invariant.)
