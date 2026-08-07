# Admin API — request-line fingerprint bans

Three operator endpoints on request-o-matic for managing request-line bans. ROM is a thin proxy: ban state lives in Backend-Service (BS#1261 `banned_fingerprints` table). This API exists so operators can manage bans without writing SQL or learning Backend-Service's better-auth admin flow.

## When to use this vs the Slack-native ban button (#152)

The **"Ban requester" button** on each Slack request post is the primary operator UX — one click, a reason prompt, done, no curl required — **once `SLACK_USE_BOT_TOKEN=true` in the target environment** (see the caveat below). This HTTP API stays useful for: ad-hoc scripts, bulk operations, and as a backup if the Slack app or the `/slack/interactivity` endpoint has an outage. Both surfaces call the same `services/ban_service.py.ban(...)`, so the audit trail is identical either way — see `banned_by_user_id` below.

## Where to find a fingerprint

**Click "Ban requester" on the Slack post — this is the supported path, once the button is live (see caveat).** The button (`services/slack.maybe_append_ban_button`) renders whenever the post's outbound `chat.postMessage` call was *given* a usable fingerprint — it has no way to know whether the transport that actually sent the message kept it. Every request to the interactivity endpoint — the click and the submission alike — has its Slack signature verified before anything else runs; an unsigned or stale one gets a flat `401` that looks identical whether or not the deployment is configured.

Clicking the button checks the acting user against `SLACK_BAN_AUTHORIZED_USERS` (see [`docs/env-vars.md`](env-vars.md)) **before** the modal opens. An unauthorized click gets an ephemeral refusal and no modal — the modal carries the listener's fingerprint in its `private_metadata`, so opening it for anyone who asked would hand out the device UUID this runbook otherwise takes care never to display. Authorization is then re-checked on submission, so the refusal is not something a crafted payload can skip.

Submitting the modal:

1. Re-verifies the signature and the acting user's authorization, independently of the click.
2. Reads the fingerprint from the clicked message's own `chat.postMessage` metadata (never from anything typed into the modal) and calls `services/ban_service.py.ban(fingerprint, reason, actor=None)` — the same function this HTTP API uses (`actor` is always `None` from rom; see that function's docstring for why).
3. Posts an ephemeral confirmation to the clicking DJ, and edits the original message with a "🚫 Banned by @dj — reason" footer so the whole channel sees the outcome. On an unusually long post the footer is skipped and the ephemeral confirmation says so — the ban still lands, and the original message is left untouched rather than being replaced by a footer-only stub.

**Caveat: the button is only live on the bot-token transport.** `chat.postMessage` `metadata` (#209, which the button depends on) is silently dropped by the incoming-webhook transport — see "The fingerprint is never displayed, only acted on" below. Until an environment has `SLACK_USE_BOT_TOKEN=true`, its buttons render but have nothing to act on; clicking one gets an ephemeral explaining that and pointing back to the PostHog fallback, rather than silently doing nothing. Staging carries the full ban stack already; production's cutover (flipping the flag and repointing the Slack app's interactivity Request URL) is a manual post-merge step, not a code change — see [#152](https://github.com/WXYC/request-o-matic/issues/152)'s acceptance criteria.

Posts with **no button at all** have no usable fingerprint by construction — an unauthenticated caller, a pre-3.2 iOS client, or a degraded post. For those, for a pre-cutover environment, or if the interactivity endpoint is down, fall back to the PostHog query below.

### PostHog fallback (when there's no button, the button isn't live yet, or the endpoint is down)

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

### What this fallback query does not cover

- **Malformed fingerprints.** A caller sending anything that isn't a UUID is treated as though it sent no header at all, here and everywhere else in ROM. That is deliberate — a non-UUID cannot be banned, because `POST /admin/bans` types the field as `UUID` and rejects it — so a junk-sending client never appears in the query above. It is not entirely invisible, though: when `STRICT_FINGERPRINT_FOR_KNOWN_CLIENTS` is on and the caller's `User-Agent` claims a known strict client (iOS 3.2+), the request is rejected `403` and emits a `request_blocked` event with `ban_reason='ua_gate_malformed_fingerprint'`, plus a bounded `fingerprint_prefix` and `fingerprint_length` (WXYC/request-o-matic#226). That tells you someone is probing, but it still yields nothing bannable — the whole point is that the value isn't a UUID. Junk from an unknown `User-Agent`, or from anyone at all while that flag is off, stays fully invisible.
- **Clients that send no fingerprint.** iOS 3.1 and older, browsers, and `curl` don't send the header, so they never appear.

### The fingerprint is never displayed, only acted on

Request posts in Slack do **not** display the listener's fingerprint as visible text — it rides as private `chat.postMessage` `metadata` ([#209](https://github.com/WXYC/request-o-matic/issues/209)), read programmatically by the button's click handler (`routers/slack_interactivity.py`), not by a human scrolling the channel. Do not go looking for it in the rendered message; click the button instead, or use the PostHog query above if there is no button.

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

`banned_by_user_id` is always `null` from request-o-matic — for HTTP admin callers because they're identified only by `ADMIN_TOKEN`, not an `auth_user.id`, and for Slack-triggered bans because a Slack user ID has no corresponding better-auth `user` row for Backend-Service's foreign key to reference (BS's own schema comment on `banned_fingerprints` calls this out explicitly). The Slack actor is recorded in Slack itself instead: the button posts an ephemeral ack to the clicking DJ and edits the original message with a "banned by @dj — reason" footer.

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

## Environment variables

See [`docs/env-vars.md`](env-vars.md) for the canonical reference. The three vars this API needs:

- `ADMIN_TOKEN` — bearer for the operator side.
- `BS_INTERNAL_BANS_URL` — base URL of BS's CRUD.
- `BS_INTERNAL_KEY` — shared secret with BS's `ROM_INTERNAL_KEY`.

## Related

- Storage: [WXYC/Backend-Service#1261](https://github.com/WXYC/Backend-Service/issues/1261) — `banned_fingerprints` table + `/internal/banned-fingerprints` CRUD.
- Request-time enforcement: [#150](https://github.com/WXYC/request-o-matic/issues/150).
- Slack-native ban button: [#152](https://github.com/WXYC/request-o-matic/issues/152). Shipped — see "Where to find a fingerprint" above, and the transport caveat there for when it goes live in a given environment.
