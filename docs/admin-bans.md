# Admin API — request-line fingerprint bans

Three operator endpoints on request-o-matic for managing request-line bans. ROM is a thin proxy: ban state lives in Backend-Service (BS#1261 `banned_fingerprints` table). This API exists so operators can manage bans without writing SQL or learning Backend-Service's better-auth admin flow.

## When to use this vs Slack-native ban (#152)

The Slack-native action (filed at #152) is the primary operator UX once it lands — one click in the Slack post itself, no curl required. This HTTP API stays useful for: ad-hoc scripts, bulk operations, and as a backup if the Slack app has an outage.

## Where to find a fingerprint

**Not yet available.** Request posts in Slack do not currently carry the listener's fingerprint, and normal-request telemetry does not record it — it appears only on `request_blocked` events, i.e. only for devices that are already banned. Surfacing it is tracked in [WXYC/request-o-matic#216](https://github.com/WXYC/request-o-matic/issues/216) (fingerprint on telemetry — the interim lookup path, no dependencies) and [WXYC/request-o-matic#152](https://github.com/WXYC/request-o-matic/issues/152) (an in-Slack ban button — the real one). Until that lands, the endpoints below work but there is no supported way to obtain a fingerprint to pass to them.

There is intentionally **no** "discover fingerprints" endpoint — the design point is that operators identify a listener through the same Slack post they're already reacting to.

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

`banned_by_user_id` is `null` for HTTP admin callers (they're identified only by `ADMIN_TOKEN`, not by an `auth_user.id`). The Slack-native router (#152) populates it.

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
| 400 | Malformed input (bad UUID, empty reason, expired-at out of range) | Fix the request body and retry |
| 401 | Missing `Authorization` header | Add the bearer header |
| 403 | Wrong token, or `ADMIN_TOKEN` not configured server-side | Check the token; if disabled, set `ADMIN_TOKEN` on the Railway service |
| 422 | FastAPI rejected a query param (e.g. `limit=500`) | Use a valid value |
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
- Slack-native ban (planned): [#152](https://github.com/WXYC/request-o-matic/issues/152).
