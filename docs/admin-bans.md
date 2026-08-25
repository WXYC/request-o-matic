# Admin API — request-line fingerprint bans

Three operator endpoints on request-o-matic for managing request-line bans. ROM is a thin proxy: ban state lives in Backend-Service (BS#1261 `banned_fingerprints` table). This API exists so operators can manage bans without writing SQL or learning Backend-Service's better-auth admin flow.

## When to use this vs the Slack-native ban menu (#152)

The **"Ban requester" item** in the overflow ("...") menu on each Slack request post is the primary operator UX — open the menu, pick it, give a reason, done, no curl required — **once `SLACK_USE_BOT_TOKEN=true` in the target environment** (see the caveat below). This HTTP API stays useful for: ad-hoc scripts, bulk operations, and as a backup if the Slack app or the `/slack/interactivity` endpoint has an outage. Both surfaces call the same `services/ban_service.py.ban(...)`, so the audit trail is identical either way — see `banned_by_user_id` below.

## Where to find a fingerprint

**Open the "..." menu on the Slack post and choose "Ban requester" — this is the supported path, once the menu is live (see caveat).** The menu (`services/slack.maybe_append_ban_button`) renders whenever the post's outbound `chat.postMessage` call was *given* a usable fingerprint — it has no way to know whether the transport that actually sent the message kept it. Every request to the interactivity endpoint — the click and the submission alike — has its Slack signature verified before anything else runs; an unsigned or stale one gets a flat `401` that looks identical whether or not the deployment is configured.

Choosing the menu item checks the acting user against `SLACK_BAN_AUTHORIZED_USERS` (see [`docs/env-vars.md`](env-vars.md)) **before** the modal opens. An unauthorized click gets an ephemeral refusal and no modal — the modal carries the listener's fingerprint in its `private_metadata`, so opening it for anyone who asked would hand out the device UUID this runbook otherwise takes care never to display. Authorization is then re-checked on submission, so the refusal is not something a crafted payload can skip.

Submitting the modal:

1. Re-verifies the signature and the acting user's authorization, independently of the click.
2. Reads the fingerprint from the clicked message's own `chat.postMessage` metadata (never from anything typed into the modal) and calls `services/ban_service.py.ban(fingerprint, reason, actor=None)` — the same function this HTTP API uses (`actor` is always `None` from rom; see that function's docstring for why).
3. Posts an ephemeral confirmation to the clicking DJ, and edits the original message with a "🚫 Banned by @dj — reason" footer so the whole channel sees the outcome. On an unusually long post the footer is skipped and the ephemeral confirmation says so — the ban still lands, and the original message is left untouched rather than being replaced by a footer-only stub.

**Caveat: the menu is only live on the bot-token transport.** `chat.postMessage` `metadata` (#209, which the menu depends on) is silently dropped by the incoming-webhook transport — see "The fingerprint is never displayed, only acted on" below. Until an environment has `SLACK_USE_BOT_TOKEN=true`, its menus render but have nothing to act on; choosing the item gets an ephemeral explaining that and pointing back to the PostHog fallback, rather than silently doing nothing. Staging carries the full ban stack already; production's cutover (flipping the flag and repointing the Slack app's interactivity Request URL) is a manual post-merge step, not a code change — see [#152](https://github.com/WXYC/request-o-matic/issues/152)'s acceptance criteria.

Posts with **no menu at all** have no usable fingerprint by construction — an unauthenticated caller, a pre-3.2 iOS client, or a degraded post. For those, for a pre-cutover environment, or if the interactivity endpoint is down, fall back to the PostHog query below.

### PostHog fallback (when there's no menu, the menu isn't live yet, or the endpoint is down)

Both `request_completed` (a message the parser classified as a song request) and `request_non_request` (a message it classified as feedback, a DJ shout-out, or other chatter — WXYC/request-o-matic#228) carry a `fingerprint` property whenever the client sent a well-formed `X-Device-Fingerprint` UUID. Run this in the **Request-O-Matic** PostHog project (SQL tab) to see per-device request counts across both event types, most active first. Request-o-matic reports to its own project — it is *not* the WXYC iOS one — and running this query in the wrong project returns zero rows, which reads identically to "the fingerprint was never recorded":

```sql
SELECT properties.fingerprint AS fp, count() AS requests, max(timestamp) AS last_seen
FROM events
WHERE event IN ('request_completed', 'request_non_request')
  AND timestamp > now() - INTERVAL 7 DAY
  AND properties.fingerprint IS NOT NULL
GROUP BY fp
ORDER BY requests DESC
```

Copy the `fp` value for the offending device into `POST /admin/bans` below. It is a full UUID by construction: the router records the value only after `services/fingerprint.normalize_fingerprint` accepts it, so anything this query returns is something `POST /admin/bans` will take.

### Log probe (when PostHog itself is untrustworthy)

The PostHog query above has a failure mode that looks exactly like a result: if ingestion is down, it returns zero rows, which reads identically to "no client ever sent a fingerprint". That is not hypothetical -- during the 2026-08-04 quota exhaustion the `fingerprint` property added in [#216](https://github.com/WXYC/request-o-matic/issues/216) was never ingested even once, because it shipped on 2026-08-07, *after* ingestion stopped. Anyone running the query in that window would have concluded no client supports fingerprints.

Production logs answered the same question throughout. Every request that passes the empty-message check emits one line before any other branching (WXYC/request-o-matic#278) -- a blank-message client is rejected with a `400` upstream of it and leaves no line, so this probe counts inbound *work*, not inbound *connections*:

```
Request received (user_agent=WXYC-iOS/3.2.1, fingerprint=present)
```

`fingerprint` is one of `present` (a UUID `POST /admin/bans` will accept), `malformed` (the header arrived but is not a UUID -- **including empty**, which means a client that tried and failed), or `absent` (no header at all, which is what a pre-3.2 client looks like). The value itself is never logged. That `malformed`/`absent` line is drawn the same way on `request_blocked`, so the two event types can be unioned safely.

```bash
railway logs --service request-o-matic --environment production \
  | grep "Request received"
```

A second, independent probe needs no new logging at all: a `POST https://api.wxyc.org/auth/check-request-ban` line appears when the request carried `authorization or normalized_fingerprint` (`routers/request.py`).

**Confirm enforcement is on before reading anything into its absence.** The call is also gated on `ban_check_client is not None`, which `core/dependencies.get_ban_check_client` returns as `None` whenever `ENFORCE_REQUEST_BANS` is off *or* `BS_CHECK_REQUEST_BAN_URL` is unset. With either true, **zero** ban-check lines appear no matter what headers arrived, and the probe silently reports the same thing it reports when no client is bannable. A BS network failure has the same shape for a different reason: `BanCheckClient` raises before httpx logs the request line.

So absence is evidence only against a positive control. Fire a synthetic request carrying a known-good UUID first; if that produces a ban-check line, the config is live and absence against real traffic then does mean those requests were unbannable:

```bash
railway logs --service request-o-matic --environment production \
  | grep -E "Parsing message:|auth/check-request-ban"
```

Both probes are log-based, so they keep working when analytics do not. Reach for them before trusting a zero from PostHog.

### What this fallback query does not cover

- **Malformed fingerprints.** A caller sending anything that isn't a UUID is treated as though it sent no header at all, here and everywhere else in ROM. That is deliberate — a non-UUID cannot be banned, because `POST /admin/bans` types the field as `UUID` and rejects it — so a junk-sending client never appears in the query above. It is not entirely invisible, though: when `STRICT_FINGERPRINT_FOR_KNOWN_CLIENTS` is on and the caller's `User-Agent` claims a known strict client (iOS 3.2+), the request is rejected `403` and emits a `request_blocked` event with `ban_reason='ua_gate_malformed_fingerprint'`, plus a bounded `fingerprint_prefix` and `fingerprint_length` (WXYC/request-o-matic#226). That tells you someone is probing, but it still yields nothing bannable — the whole point is that the value isn't a UUID. Junk from an unknown `User-Agent`, or from anyone at all while that flag is off, stays fully invisible.
- **Clients that send no fingerprint.** iOS 3.1 and older, browsers, and `curl` don't send the header, so they never appear.

### The fingerprint is never displayed, only acted on

Request posts in Slack do **not** display the listener's fingerprint as visible text — it rides as private `chat.postMessage` `metadata` ([#209](https://github.com/WXYC/request-o-matic/issues/209)), read programmatically by the menu's click handler (`routers/slack_interactivity.py`), not by a human scrolling the channel. Do not go looking for it in the rendered message; use the menu instead, or the PostHog query above if there is no menu.

## Authentication

All three endpoints require:

```
Authorization: Bearer $ADMIN_TOKEN
```

The token is whatever's set as `ADMIN_TOKEN` on the Railway service. If unset on the server, every admin request is rejected with 403 (fail-closed). Rotate by updating the Railway variable; there is no per-user token.

## Endpoints

All paths are relative to the request-o-matic base URL — for production:
```
https://request-o-matic-production.up.railway.app
```

For staging:
```
https://request-o-matic-staging.up.railway.app
```

### POST /admin/bans — create or update a ban

Idempotent. Re-banning an already-banned fingerprint succeeds with 200 and returns the updated record.

Request body:

```json
{
  "fingerprint": "11111111-2222-3333-4444-555555555555",
  "reason": "spamming the request line",
  "expires_in_seconds": 86400
}
```

- `fingerprint` (string, required) — the listener's device UUID. See "Where to find a fingerprint" above for how to obtain one.
- `reason` (string, required) — operator-visible reason. Capped at 1000 chars by Backend-Service.
- `expires_in_seconds` (integer, optional) — auto-expiry. Omit for a permanent ban.

Response (200):

```json
{
  "fingerprint": "11111111-2222-3333-4444-555555555555",
  "banned_at": "2026-05-31T18:00:00.000Z",
  "ban_reason": "spamming the request line",
  "ban_expires_at": "2026-06-01T18:00:00.000Z",
  "banned_by_user_id": null
}
```

`banned_by_user_id` is always `null` from request-o-matic — for HTTP admin callers because they're identified only by `ADMIN_TOKEN`, not an `auth_user.id`, and for Slack-triggered bans because a Slack user ID has no corresponding better-auth `user` row for Backend-Service's foreign key to reference (BS's own schema comment on `banned_fingerprints` calls this out explicitly). The Slack actor is recorded in Slack itself instead: the menu's handler posts an ephemeral ack to the acting DJ and edits the original message with a "banned by @dj — reason" footer.

Curl:

```bash
curl -X POST "https://request-o-matic-production.up.railway.app/admin/bans" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "fingerprint": "11111111-2222-3333-4444-555555555555",
    "reason": "spamming the request line",
    "expires_in_seconds": 86400
  }'
```

### DELETE /admin/bans/{fingerprint} — remove a ban

Idempotent. Unbanning a fingerprint that isn't banned still returns 204. Returns 204 with no body on success.

Curl:

```bash
curl -X DELETE \
  "https://request-o-matic-production.up.railway.app/admin/bans/11111111-2222-3333-4444-555555555555" \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

### GET /admin/bans — list bans (keyset-paginated)

Query params:

- `limit` (integer, optional, default 50, range 1–200) — max items per page.
- `cursor` (string, optional) — opaque cursor from a prior response's `nextCursor`.

Response (200):

```json
{
  "items": [
    {
      "fingerprint": "11111111-2222-3333-4444-555555555555",
      "banned_at": "2026-05-31T18:00:00.000Z",
      "ban_reason": "spamming the request line",
      "ban_expires_at": null,
      "banned_by_user_id": null
    }
  ],
  "nextCursor": "2026-05-31T18:00:00.000Z|11111111-2222-3333-4444-555555555555"
}
```

`nextCursor` is `null` on the last page. To fetch the next page, pass it back verbatim.

Curl:

```bash
curl -G "https://request-o-matic-production.up.railway.app/admin/bans" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  --data-urlencode "limit=25"
```

## Status codes

| Code | When | What to do |
|------|------|------------|
| 200 / 204 | Success | — |
| 400 | Backend-Service rejected the input and rom forwarded its status verbatim | Read `detail.upstream_body`, fix the request, retry |
| 401 | Missing `Authorization` header | Add the bearer header |
| 403 | Wrong token, or `ADMIN_TOKEN` not configured server-side | Check the token; if disabled, set `ADMIN_TOKEN` on the Railway service |
| 422 | rom rejected the request locally, before calling BS: non-UUID `fingerprint`, empty or over-long `reason`, non-positive `expires_in_seconds`, an unknown field, or a bad query param (e.g. `limit=500`) | Fix the request body or param and retry |
| 502 | Backend-Service upstream returned 5xx | Check BS health; retry after |
| 503 | `BS_INTERNAL_BANS_URL` or `BS_INTERNAL_KEY` not configured | Set the missing env var on the Railway service |

## Managing moderators — `/request-mods` ([#240](https://github.com/WXYC/request-o-matic/issues/240))

Run `/request-mods` in Slack. A picker opens with the current moderators pre-selected; add or remove people using Slack's own workspace autocomplete and hit **Save**. The change takes effect on the next ban click — **no deploy, no Railway access**.

You must already be authorized to ban in order to open it. A non-moderator gets an ephemeral refusal that names nobody.

### Who can ban is a union of two lists

| | Where it lives | Who edits it | What it's for |
|---|---|---|---|
| **Moderator roster** | Backend-Service `slack_ban_moderators` table | any moderator, via `/request-mods` | the actual roster; turns over every semester as MDs rotate |
| **`SLACK_BAN_AUTHORIZED_USERS`** | Railway env var, per ROM environment | anyone with Railway access | **break-glass only** — a small set of administrators, trimmed to that on 2026-08-08 |

Authorization is the union, so a person on either list can ban. That is what makes the design safe to operate: **if Backend-Service is unreachable, or the table is emptied by accident, the break-glass list still works** and nobody is locked out of their own moderation tool by an upstream outage.

The picker edits the table only. Environment-allowlist members appear in a read-only block beneath it, because otherwise the modal would lie — deselecting an administrator would appear to work and change nothing.

**Moving someone between the two lists is not atomic, and the order matters.** Removing an administrator from `SLACK_BAN_AUTHORIZED_USERS` revokes their access the moment the service restarts; adding them to the roster grants it on their next click. Add first, then trim — the reverse leaves a window where they cannot ban, and nothing anywhere reports it, because a union that has shrunk looks exactly like a person who was never authorized. This is also why the two lists are checked separately below rather than assumed to agree.

### The roster is single, not per-environment

Both ROM environments point `BS_INTERNAL_MODERATORS_URL` at *production* Backend-Service under a shared key — staging has no Backend-Service of its own. Editing the roster from staging edits the production roster. This matches the existing posture for `BS_INTERNAL_BANS_URL`.

### Checking who can actually ban

The modal is the everyday answer, but it renders the roster — it is not an independent check of it. To read both halves from outside Slack:

```bash
# Break-glass half (per environment; both should match).
railway variable list --service request-o-matic --environment production --json \
  | jq -r '.SLACK_BAN_AUTHORIZED_USERS'

# Roster half. Read the key into the environment rather than pasting it, so it
# stays out of your shell history and the terminal.
export BS_INTERNAL_KEY=$(railway variable list --service request-o-matic \
  --environment production --json | jq -r '.BS_INTERNAL_KEY')
curl -s -H "X-Internal-Key: $BS_INTERNAL_KEY" \
  https://api.wxyc.org/internal/slack-ban-moderators | jq '.items'
```

Who can ban is the **union** of the two. Note the roster is production-only, so the second command is the same regardless of which ROM environment you asked about.

Two traps worth knowing:

- **A variable change does nothing until the service restarts.** `get_settings` is `lru_cache`d, so a trimmed allowlist stays in effect for the life of the process. Railway redeploys on a variable change by default; if you set one with deploys skipped, the old value is still live.
- **Read the roster immediately before trimming, not from earlier in the session.** It can change under you — anyone with `/request-mods` can edit it — and a stale read is how you revoke someone you believed was covered.

### When something goes wrong

| Symptom | Cause | Effect |
|---|---|---|
| "Moderator management isn't set up on this deployment" | `BS_INTERNAL_MODERATORS_URL` unset | Bans still work off the break-glass list. Set the variable. |
| "Couldn't reach the moderator list just now" | Backend-Service unreachable or slow (>1.5s) | Bans still work off the break-glass list. Retry. |
| "Someone else changed the moderator list while this was open" | Concurrent edit; your `expectedCurrent` is stale | Nothing was saved. Close and re-run `/request-mods`. |
| Modal opens with nobody pre-selected and a "could not pre-select" note | Slack rejected `initial_users`, most likely a deactivated account in the roster | Re-select everyone who should stay, then save — this is how a deactivated ID gets removed. |
| "This list opened empty because Slack could not pre-select the current moderators" | You hit **Save** on that empty retry modal without re-selecting | Nothing was changed. Saving it would have removed every moderator, so it is refused — re-select, or close and re-run. |
| "Couldn't reach the moderator list just now, so this couldn't be saved" | Backend-Service went down while your modal was open | Nothing was changed. This is deliberately *not* worded as a permissions refusal: your access is fine, the roster just couldn't be read. |
| "Moderator management is unavailable right now." | Same as the two rows above, but you are not on the break-glass list | The specific cause is withheld from non-administrators on purpose. Ask someone on `SLACK_BAN_AUTHORIZED_USERS`. |

An unreachable Backend-Service can only ever **shrink** who can ban, never widen it.

## Environment variables

See [`docs/env-vars.md`](env-vars.md) for the canonical reference. The vars this API needs:

- `ADMIN_TOKEN` — bearer for the operator side.
- `BS_INTERNAL_BANS_URL` — base URL of BS's CRUD.
- `BS_INTERNAL_MODERATORS_URL` — base URL of BS's moderator roster, backing `/request-mods`. Unset degrades to the break-glass allowlist; it does **not** 503 the way the bans URL does.
- `BS_INTERNAL_KEY` — shared secret with BS's `ROM_INTERNAL_KEY`, used by both `/internal` surfaces.

## Related

- Storage: [WXYC/Backend-Service#1261](https://github.com/WXYC/Backend-Service/issues/1261) — `banned_fingerprints` table + `/internal/banned-fingerprints` CRUD.
- Moderator roster storage: [WXYC/Backend-Service#2045](https://github.com/WXYC/Backend-Service/issues/2045) — `slack_ban_moderators` table + `/internal/slack-ban-moderators` GET/PUT.
- Request-time enforcement: [#150](https://github.com/WXYC/request-o-matic/issues/150).
- Slack-native ban menu: [#152](https://github.com/WXYC/request-o-matic/issues/152). Shipped — see "Where to find a fingerprint" above, and the transport caveat there for when it goes live in a given environment.
- Moderator roster UI: [#240](https://github.com/WXYC/request-o-matic/issues/240).
