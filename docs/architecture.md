# Architecture

## Request Flow
1. **UA gate (optional)**: When `STRICT_FINGERPRINT_FOR_KNOWN_CLIENTS=true` and the request's `User-Agent` matches a registered WXYC client at-or-above its strict-mode version (currently `WXYC-iOS >= 3.2`, registry in `services/ua_gate.py`), a missing *or malformed* `X-Device-Fingerprint` triggers a 403 *before* the BS round-trip. The header is normalized through `services/fingerprint.normalize_fingerprint` first, so "present but not a UUID" is rejected exactly like "absent" -- gating on bare presence let `X-Device-Fingerprint: not-a-uuid` with no `Authorization` clear this gate and then normalize to no-signal at the ban check, passing both defenses. Independent of the ban-check flag — this is a structural requirement on known clients, not a ban decision. Unknown UAs (browsers, curl, v3.1 iOS) keep the existing lenient contract. See [`docs/env-vars.md`](env-vars.md) for the flip sequence.
2. **Ban check (optional)**: When `ENFORCE_REQUEST_BANS=true` and the caller supplies `Authorization` and/or `X-Device-Fingerprint`, ROM consults Backend-Service's `POST /auth/check-request-ban` ([BS#1261](https://github.com/WXYC/Backend-Service/issues/1261)) before parsing. Banned callers get 403 with no Slack, no Groq, no LML; everyone else proceeds. See `services/ban_check_client.py` and [`docs/env-vars.md`](env-vars.md) for the full flow + flag.
3. **Parse**: Groq AI (`openai/gpt-oss-20b`) extracts artist/song/album from message
4. **Early return**: Non-request messages (feedback, DJ messages) are posted to Slack without search
5. **Delegate**: Search is delegated to [library-metadata-lookup](https://github.com/WXYC/library-metadata-lookup) via HTTP (`LOOKUP_SERVICE_URL`).
6. **Filter to library**: Non-library results are dropped. LML can surface albums not in the WXYC catalog as row-less items (LML#631: `library_item.id == 0`, empty `library_url`, `call_number="(external)"`); a DJ can't pull a non-shelved album, so the request channel keeps only real library releases (`library_item.id > 0`). If this leaves nothing, the Slack post falls through to the "No results found" message.
7. **Slack**: Post enriched results with artwork to Slack

### Fingerprint metadata on Slack posts ([#209](https://github.com/WXYC/request-o-matic/issues/209))
Every Slack post carries the requester's `X-Device-Fingerprint`, when present, as private `chat.postMessage` metadata -- never as visible block text. `services/slack.build_slack_metadata(fingerprint)` builds the envelope `{"event_type": "request_posted", "event_payload": {"fingerprint": "<uuid>"}}` (the `event_type` string is `services.slack.SLACK_METADATA_EVENT_TYPE`); it returns `None` when there is no *usable* fingerprint so the `metadata` key is omitted entirely rather than sent empty. "Usable" means "passes `services/fingerprint.normalize_fingerprint`" -- a UUID, which is what `POST /admin/bans` accepts. FastAPI binds a present-but-empty header to `""` (not `None`), so an `is None` guard alone would attach `{"fingerprint": ""}` and leave #152 rendering a ban menu that 422s on every click; bounding the value also keeps a caller-controlled string out of `chat.postMessage`, whose `metadata_too_large` / `invalid_metadata_format` errors arrive as HTTP 200 `{"ok": false}` and cost the listener their whole request via a 502. The same helper gates `services/ban_check_client.py`, so the value ROM will ban on is exactly the value it advertises as bannable. `SlackService.post_blocks` only forwards `metadata` on the bot-token transport ([#215](https://github.com/WXYC/request-o-matic/issues/215)) -- incoming webhooks don't support `chat.postMessage` parameters, so it is dropped on that path. **[WXYC/request-o-matic#152](https://github.com/WXYC/request-o-matic/issues/152)'s "Ban requester" menu matches on the `request_posted` event_type to locate the fingerprint; changing that string breaks every existing menu.**

### In-Slack ban menu ([#152](https://github.com/WXYC/request-o-matic/issues/152))
`services/slack.maybe_append_ban_button` appends an actions block holding a "Ban requester" overflow ("...") menu to a post's blocks iff its fingerprint normalizes to a usable UUID -- the same usable/unusable split as `build_slack_metadata`, so the two can never disagree about whether a fingerprint was *given*. Whether the menu actually has anything to act on is a separate question: `build_slack_metadata`'s envelope is a `chat.postMessage`-only parameter, silently dropped by the incoming-webhook transport, so a webhook-posted message's menu renders but finds no fingerprint on click. `routers/slack_interactivity.py` posts an ephemeral explaining this rather than no-op'ing silently; see the caveat in [`docs/admin-bans.md`](admin-bans.md). Slack requires a `value` on every overflow option, so unlike the button this replaced the element cannot carry none; it is pinned to the inert `services.slack.BAN_MENU_OPTION_VALUE` and never read back. Choosing the item fires a `block_actions` payload at `POST /slack/interactivity`, which reads the fingerprint out of the *clicked message's own metadata*, not from anything the client set. That handler:

1. Verifies `X-Slack-Signature` (`services/slack_signature.verify_slack_signature`: `hmac.compare_digest` over `v0:{timestamp}:{body}` with `SHA256(SLACK_SIGNING_SECRET)`, plus a 5-minute replay window) before parsing anything else.
2. On a menu selection, opens a reason modal via `views.open`, stashing the fingerprint, channel, message ts, and (size permitting -- Slack caps `private_metadata` at 3000 characters) the message's own blocks minus the menu, in the modal's `private_metadata`.
3. On submit (`view_submission`), re-authorizes the acting user server-side against the **union** of `SLACK_BAN_AUTHORIZED_USERS` and the Backend-Service moderator roster (`services/slack_authorization.resolve_authorized_users` + `is_authorized_slack_user_in`; both halves empty denies everyone), validates the reason is 1-1000 characters (matching `BanCreateRequest`), then calls `services/ban_service.ban(fingerprint, reason, actor=None)` -- the identical function `routers/admin.py` calls, so a Slack click and a curl produce the same audit trail. `actor` is always `None` from rom: Backend-Service's `banned_by_user_id` foreign-keys to better-auth's `user.id`, which a Slack user has no row in, so the Slack actor is recorded in Slack itself (the ephemeral ack + the edited "banned by @dj" footer) rather than in that column.
4. Posts an ephemeral confirmation to the clicking DJ (`chat.postEphemeral`) and edits the original message (`chat.update`) to append the footer, reusing the blocks captured in step 2 when they fit.

`SLACK_SIGNING_SECRET` and `SLACK_BAN_AUTHORIZED_USERS` are the two settings this flow adds; both fail closed when unset. See [`docs/env-vars.md`](env-vars.md) and [`docs/admin-bans.md`](admin-bans.md).

### Moderator roster ([#240](https://github.com/WXYC/request-o-matic/issues/240))
`SLACK_BAN_AUTHORIZED_USERS` was the whole allowlist through #152; since #240 it is the **break-glass half** and the roster proper lives in Backend-Service's `/internal/slack-ban-moderators` table ([BS#2045](https://github.com/WXYC/Backend-Service/issues/2045)). Who can ban is the union, so no data migration was needed: on day one the environment IDs still authorize, and new moderators are added through Slack.

`/request-mods` (`POST /slack/commands`) reads the roster once and uses that single read for two things -- authorizing the invoker and populating the picker's `initial_users` -- because Slack invalidates `trigger_id` after ~3 seconds and the budget before `views.open` is one upstream round-trip. The modal carries the roster in `private_metadata` as `expectedCurrent`; the save (`view_submission`, dispatched by `routers/slack_interactivity.py` on `moderator_roster_modal`, since Slack sends every modal submission to the one interactivity URL) round-trips it back so Backend-Service can reject a concurrent edit with a 409 rather than silently discarding one of them.

Two directions matter and they differ:

- **Ban authorization fails closed to the environment allowlist.** An unreachable Backend-Service must *shrink* the authorized set, never widen it, and must never take the ban button down -- which is why `get_moderator_client` returns `None` when unconfigured rather than 503ing like `get_ban_admin_client`.
- **`/request-mods` refuses outright** on the same failure. There is no useful degraded modal: one populated from a failed read shows an empty picker, and saving that would wipe the roster.

Every Backend-Service call on this path is bounded well inside Slack's deadlines (1.5s read, 2.5s save) rather than inheriting the sibling client's 10s, and `views.open` grew a 1.0s bound applied to every caller including the shipped ban modal -- no `views.open` response arriving after `trigger_id` expires can be acted on, so waiting longer only holds a worker.

## Degraded Modes
Slack is the only hard dependency. When Groq or LML are unavailable the listener's message still reaches Slack, with a context line explaining what's missing. The response is `200` with a `degraded_mode` field:

- **`parsing_unavailable`** — Groq failed (rate limit, timeout, parse error, etc.). The raw listener message is posted to Slack with a `_Parsing unavailable_` context line. No classification, no search.
- **`search_unavailable`** — LML is down or `LOOKUP_SERVICE_URL` is unset. The parsed message is posted to Slack with a `_Search unavailable_` context line containing any artist/song/album fields Groq extracted. Empty `library_results`.
- **`ban_check_unavailable`** — BS `/auth/check-request-ban` was unreachable. The request still proceeds (fail-open is the correct posture for an authz feature; a BS outage must not break listener requests). Emitted only when no more severe degraded mode is active; the independent `ban_check_degraded` PostHog property is always set so operators see both signals together.

If Slack itself fails the endpoint returns `502` — there is no further fallback. PostHog events for degraded requests carry `degraded_mode` and `degraded_reason` (the exception class name) so outages are visible in telemetry.

## Server-Timing (observability, [Backend-Service#881](https://github.com/WXYC/Backend-Service/issues/881))
`POST /request` attaches a `Server-Timing` response header (flag `ENABLE_SERVER_TIMING`, default on) that combines ROM's own per-stage timings (`parse`, `lookup_service`, `slack_post`) with the sub-stages LML forwards in its own `Server-Timing` header (`library_search`, `metadata_enrichment`, `discogs`, and others). LML's own self-measured `total` is renamed to `lml_total` (not dropped) so the merged header keeps exactly one ROM-owned `total` while still surfacing `lookup_service - lml_total` = ROM<->LML transport + LML framework overhead. It re-surfaces the `RequestTelemetry.track_step` durations already shipped to PostHog — no new capture layer — so a caller can localize latency (e.g. an ~8.5s `metadata_enrichment` Apple-probe stall) the JSON body doesn't reveal. The merge (`routers/request._emit_server_timing_header` + `core/server_timing.parse_server_timing`) is out-of-band and can never raise into the request path. Flag reference + wire-format example: [`docs/env-vars.md`](env-vars.md).

## Key Files
- `models.py` - Re-exports `LibraryItem` (alias for `LibraryCatalogItem`) and `ReleaseMetadata` (alias for `DiscogsMatchResult`) from `generated.api_models`, plus the `preview_url(metadata)` helper for streaming-priority logic.
- `generated/api_models.py` - Pydantic v2 models generated from `wxyc-shared/api.yaml`. Do not edit by hand; regenerate via `bash scripts/generate_api_models.sh`. The script prefers a sibling `wxyc-shared/` directory and falls back to downloading `api.yaml` from GitHub.
- `tests/factories.py` - `make_library_item` / `make_release_metadata` factories that supply the now-required `call_number`, `library_url`, and `release_url` fields with sensible defaults.
- `routers/request.py` - Request handling: parse, delegate to lookup service, post to Slack
- `routers/health.py` - Health check endpoints: `GET /health` (shallow liveness probe, no external calls) and `GET /health/ready` (deep readiness check: groq, lookup, slack services). Railway's `healthcheckPath` uses `/health` so the container becomes routable immediately on boot.
- `services/parser.py` - Groq AI message parsing
- `services/lookup_client.py` - HTTP client for library-metadata-lookup delegation. `lookup()` returns a `LookupResult(response, server_timing)` pairing the parsed body with LML's raw `Server-Timing` header value (returned by value, not stashed on the shared client, to stay race-free under concurrent requests).
- `core/server_timing.py` - `parse_server_timing(header)`: stdlib-only, defensive parse of an upstream `Server-Timing` header into ordered `(name, dur_ms)` legs (None/malformed → `[]`), used to merge LML's forwarded stages into ROM's header.
- `services/ban_check_client.py` - HTTP client for Backend-Service `POST /auth/check-request-ban` (BS#1261). Fails open (raises `BanCheckUnavailableError`) on network errors, timeouts, or 5xx; treats BS 401/404 as proceed-as-unauth so the caller is never 401'd on `POST /request`.
- `services/ua_gate.py` - Pure-function matcher `is_known_strict_client(user_agent)` for the UA gate (#155). Maintains the `_STRICT_CLIENT_REGISTRY` of product-token → minimum-version pairs. Adding `WXYC-Android` here is a one-line change when that team ships ban-aware code.
- `services/fingerprint.py` - Pure-function `normalize_fingerprint(header)`: the single UUID validator for `X-Device-Fingerprint`, returning `None` for absent, empty, whitespace-only, and malformed values alike. Shared by the UA gate and ban check (`routers/request.py`), the BS ban-check client, and `services/slack.build_slack_metadata`, so the value ROM will ban on is exactly the value it advertises as bannable. Keep it one regex — a second copy is how the ban gate and the Slack envelope drift apart.
- `services/slack.py` - Slack message formatting and posting, including `maybe_append_ban_button` (#152)
- `services/slack_signature.py` - `verify_slack_signature`: stdlib `hmac`/`hashlib` verification of Slack's `X-Slack-Signature` HMAC, with a replay-window timestamp check (#152).
- `services/slack_authorization.py` - `resolve_authorized_users`: the union of the `SLACK_BAN_AUTHORIZED_USERS` break-glass allowlist and the Backend-Service moderator roster, fail-closed to the allowlist alone when the roster is unreachable or unconfigured (#152, #240). `is_authorized_slack_user_in` holds the fail-closed shape guard; `is_authorized_slack_user` remains as the CSV-only wrapper.
- `services/moderator_client.py` - HTTP client for Backend-Service's `/internal/slack-ban-moderators` roster (BS#2045). Structural mirror of `ban_admin_client.py` except for timeouts, which are cut to 1.5s (read) and 2.5s (write) because every call runs inside a Slack deadline (#240).
- `routers/slack_interactivity.py` - `POST /slack/interactivity`: the Slack app's single interactivity Request URL. Dispatches the ban menu's `block_actions` (open modal) and both `view_submission` callbacks -- the ban modal (authorize, ban, ack, edit) (#152) and the moderator roster save (#240).
- `routers/slack_commands.py` - `POST /slack/commands`: handles `/request-mods`, opening the moderator picker. Every refusal is a 200 with an ephemeral JSON body rather than a `chat.postEphemeral` call, so a deploy without `SLACK_BOT_TOKEN` refuses visibly instead of silently (#240).
- `core/dependencies.py` - FastAPI dependency injection (HTTP client, Groq, Slack, PostHog). Sentry, telemetry, cache stats, and PostHog client construction live in [`wxyc-fastapi`](https://github.com/WXYC/wxyc-fastapi); `core/dependencies.get_posthog_client` only wraps the shared singleton with the rom-side `enable_telemetry` flag.
- `core/groq_tracing.py` - Sentry instrumentation for Groq parse calls: a `groq_parse_span` context manager wrapping `services.parser.parse_request` (op `ai.parse`, tagged with model, input length, token counts, and parse-result fields), and an `install_groq_retry_breadcrumbs()` filter on the `groq._base_client` logger that converts the SDK's silent 429-retry log lines into structured `groq.retry` breadcrumbs. Installed once from `main.py` after `init_sentry`.
- `config/settings.py` - Pydantic Settings configuration

## Discogs Cache (Optional)
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
The cache ETL pipeline lives in a separate repo: [WXYC/discogs-etl](https://github.com/WXYC/discogs-etl). See that repo for setup instructions. The SQL schema files in `discogs-etl/schema/` define the shared contract between the ETL pipeline and this service's `cache_service.py`.

## Library ETL
The `library.db` SQLite database is synced daily from the WXYC MySQL database (Kattare) by the ETL pipeline in [WXYC/discogs-etl](https://github.com/WXYC/discogs-etl). The pipeline queries MySQL via the MariaDB `mysql` CLI (required for MySQL 4.1 old-password auth), converts the TSV output to SQLite with FTS5 via `scripts/tsv_to_sqlite.py`, enriches it with streaming links from `streaming_availability.db` (a GitHub Release artifact built by library-metadata-lookup's streaming availability pipeline), and uploads the result to the LML staging and production services via `POST /admin/upload-library-db`. See `discogs-etl/scripts/sync-library.sh` and `discogs-etl/.github/workflows/sync-library.yml` for details.

## Common Issues and Fixes

Search logic (artist matching, ambiguous format handling, compilation search) lives in the [library-metadata-lookup](https://github.com/WXYC/library-metadata-lookup) service. See that repo's documentation for details on false positive filtering, ambiguous format handling, and compilation search.
