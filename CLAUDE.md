# Claude Code Instructions for Request-O-Matic

Request-O-Matic is a FastAPI service for WXYC radio that processes song requests. It parses natural-language listener messages with Groq AI, delegates search to [library-metadata-lookup](https://github.com/WXYC/library-metadata-lookup) (LML), and posts enriched results to Slack.

## Topic guides

CLAUDE.md is a router for the always-loaded reference card. Topic depth lives in `docs/`:

- **[`docs/architecture.md`](docs/architecture.md)** — Request flow (parse → delegate → Slack), degraded modes (`parsing_unavailable`, `search_unavailable`), `Server-Timing` observability header (LML sub-stage forward/merge, BS#881), key files, optional Discogs PG cache, daily library.db ETL pipeline, pointer to LML for search-logic details
- **[`docs/env-vars.md`](docs/env-vars.md)** — Full environment-variable reference (Groq, LML, Slack, Sentry, PostHog, telemetry/integration toggles, `ENABLE_SERVER_TIMING`, admin-bans API)
- **[`docs/admin-bans.md`](docs/admin-bans.md)** — Operator runbook for `/admin/bans` (request-line ban management), the in-Slack "Ban requester" menu (`POST /slack/interactivity`, #152), and the `/request-mods` moderator roster (`POST /slack/commands`, #240): endpoints, curl examples, status codes, where to find a fingerprint, who can ban and what happens when the roster upstream is down. Both the HTTP admin router (#151) and the Slack-native router (#152) share `services/ban_service.py`. Ban authorization is the union of `SLACK_BAN_AUTHORIZED_USERS` (break-glass) and the Backend-Service roster, failing closed to the former.
- **[`docs/testing.md`](docs/testing.md)** — Unit / integration / performance test layout, pytest markers (`external_api`, `slow`, `contract`), the conditional 03:00 ET nightly NLP check and its watched-path gate, TEST_ENV configuration, local server testing, bug-fix protocol
- **[`docs/deployment.md`](docs/deployment.md)** — Railway hosting, branch → environment mapping, dependency management (`uv.lock` source of truth, generated `requirements*.txt`, regenerate/bump procedure, `deps-sync` gate), CI pin maintenance (Railway CLI version, workflow `permissions:`, `@gha/v1` reusable refs)
- **[`docs/scripts.md`](docs/scripts.md)** — `scripts/install-lookup.sh` (install `lookup` as a global command), `scripts/lookup.py`, `scripts/repl.py`, `scripts/create_posthog_dashboard.py`, CI helpers (`scripts/nlp_nightly_gate.py`, `scripts/wait_for_railway_deployment.sh`)

Read the relevant topic doc before doing work in that area.

## Running locally

```bash
uvicorn main:app --reload
```

Branches: **`main`** → staging on push; **`prod`** → production on push. Develop on `main`, verify in staging, merge `main` → `prod` to deploy production. Full bug-fix protocol (paired unit + integration coverage on every lookup bug) in [`docs/testing.md`](docs/testing.md).

## Code Style

- Line length: 100 chars
- Format with `ruff format`, lint with `ruff check` — CI gates on `ruff format --check .` and `ruff check .` (`black` is installed as a dev dependency but is **not** what CI enforces; its assert-wrapping style differs from `ruff format` and will produce a failing diff)
- Type hints encouraged but not enforced
- Async/await for all I/O operations

## Relationship to Other Repos

- **[library-metadata-lookup](https://github.com/WXYC/library-metadata-lookup)** -- The downstream search service. This repo is the caller: every `/request` invokes LML's `POST /api/v1/lookup` (gated by `LML_API_KEY` when LML's `LML_REQUIRE_AUTH=true`). Search logic, Discogs cache fallthrough, strategy pipeline, and lookup-side env vars all live there.
- **[wxyc-shared](https://github.com/WXYC/wxyc-shared)** -- Shared API contract (`api.yaml`). Defines `LookupRequest`, `LookupResponse`, `LibraryCatalogItem`, `DiscogsMatchResult`. Python models regenerate via `bash scripts/generate_api_models.sh` (committed to `generated/api_models.py`); `models.py` re-exports them under the legacy `LibraryItem` / `ReleaseMetadata` names plus the `preview_url()` streaming-priority helper.
- **[wxyc-fastapi](https://github.com/WXYC/wxyc-fastapi)** -- Shared FastAPI scaffolding. Owns Sentry init, telemetry, cache stats, and the process-wide PostHog client singleton; this repo's `core/dependencies.get_posthog_client` only wraps it with the rom-side `enable_telemetry` flag. `RequestTelemetry.as_server_timing` (>=1.1.0) serializes tracked steps into the `/request` `Server-Timing` header.
- **[discogs-etl](https://github.com/WXYC/discogs-etl)** -- ETL pipeline that builds `library.db` from the WXYC MySQL catalog and uploads it daily to LML staging + production via `POST /admin/upload-library-db`. Also owns the Discogs PG cache schema consumed (optionally) by this service via `DATABASE_URL_DISCOGS`.

## Example Music Data for Tests

WXYC is a freeform station. When creating test fixtures or mock data, use representative artists instead of mainstream acts like Queen, Radiohead, or The Beatles. The canonical data source is `wxyc-shared/src/test-utils/wxyc-example-data.json`.

Preferred defaults for fixtures:
- `ParsedRequest`: `artist="Juana Molina", song="la paradoja", album="DOGA"`
- `LibraryItem`: `artist="Stereolab", title="Aluminum Tunes", genre="Rock"`
- Other good choices: Cat Power / "Moon Pix" (Matador), Jessica Pratt / "On Your Own Love Again" (Drag City), Chuquimamani-Condori / "Edits" (self-released), Duke Ellington & John Coltrane / "Duke Ellington & John Coltrane" (Impulse Records), Sessa / "Pequena Vertigem de Amor" (Mexican Summer), Large Professor / "1st Class" (Matador Records)
