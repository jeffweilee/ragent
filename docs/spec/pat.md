# PAT — Personal Access Token authorization & lifecycle (T-PAT)

> Companion to `docs/00_spec.md §4.1` (endpoint) and `§4.6.9` (env). Scope: the
> PAT **service** in ragent — authorization write + on-demand resolution of a
> valid PAT. Calling an upstream *with* the PAT is the consumer's concern; the
> only consumer wired in this cycle is the `/brainagent/v1` proxy (fail-open).
>
> **Authorization requires an SSO id token**, so the PAT slice is only usable
> under a JWT auth mode (`RAGENT_AUTH_MODE=jwt_header` / `jwt_prefer_header`) —
> a trust-header deployment has no id token to forward to the init service.

## 1. Model

- **One nt ⇄ one PAT.** The PAT is a JWT the SSO service signs (12 h lifetime),
  a single self-rotating credential shared across all upstreams (the PAT maps to
  one system = our agent).
- **DB is the source of truth**, table `pat` (one row per `user_id`,
  `user_id` UNIQUE): `pat_cipher` (AES-256-GCM envelope), `status` ∈
  {`active`, `invalid`}. **Every successful refresh rewrites the row** — the DB
  always holds the *current* token, never a stale "initial" one.
- **Redis is a cache of the DB row.** Key `ragent:pat:{nt}` → encrypted PAT,
  `TTL = REDIS_PAT_TTL_SECONDS` (41 400 s = 11.5 h). All redis ops are
  fail-soft: a redis blip degrades to a DB read, never a 500.
- **Encryption** reuses the process `KeyManager` DEK (`RAGENT_KEK_BASE64` /
  `RAGENT_ENCRYPTED_DEK_BASE64`) — the same AES-256-GCM envelope as attachments.

## 2. Local validity (no upstream round-trip)

A PAT is *valid* when, checked locally against `PAT_PUBLIC_KEY`:
`exp` not passed **and** `iss == PAT_ISS` **and** `aud == PAT_AUD` **and** the
signature verifies (alg `PAT_JWT_ALG`, default `RS256`). Refresh is triggered
**only** by a locally-expired `exp` — lazy, because an expired PAT is still
refreshable and the upstream grants a short grace on the just-rotated token, so
no proactive refresh is needed.

## 3. Part 1 — authorization write (`POST /pat/v1/authorize`)

**The request carries no body.** SSO identity comes from the request header
(`Depends(get_user_id)` → nt) and the inbound SSO **id token** is read from
`RAGENT_JWT_HEADER` (the same header the JWT middleware verified — the token
itself survives on `request.headers`). ragent mints the PAT itself via the init
service; the FE never handles a PAT. Steps:

1. **Mint** — `PatInitClient.init(id_token)` (§3.1). A missing id token short-
   circuits to `401 PAT_REAUTH_REQUIRED` without calling the service.
2. Verify the minted PAT (§2).
3. **Binding check**: `PAT[PAT_NT_KEY_NAME] == resolved nt` (never trust an
   upstream-supplied identity — init is trusted to mint, not to name the owner).
   Mismatch → `401 PAT_REAUTH_REQUIRED`.
4. Encrypt → `repo.upsert(nt, cipher)` (`INSERT … ON DUPLICATE KEY UPDATE`,
   `status='active'` — re-authorization overwrites and reactivates an
   `invalid` row) → `cache.put(nt, cipher)`.

`204` on success. Failure writes nothing.

### 3.1 Init call — `PatInitClient.init(id_token) → patToken`

`POST {PAT_INIT_API_URL}/api/pat/token`, `content-type: application/json`, with
three headers and a date body:

| Header (name from env) | Value |
|---|---|
| `PAT_INIT_API_TOKEN_HEADER_KEY_NAME` | `PAT_INIT_API_TOKEN` (service credential) |
| `PAT_INIT_AUTHORIZE_HEADER_KEY_NAME` | the caller's inbound SSO id token |
| `PAT_INIT_SSO_HEADER_KEY_NAME`       | `PAT_INIT_SSO_SITE_URL` |

Body `{"expireDate": "YYYY/MM/DD"}` — `today + PAT_INIT_EXPIRE_DAYS` (default
360, a margin under the API's **one-year ceiling**). Response `{"patToken": …}`.

**Error matrix** (responses from the init API):

| Status | Meaning | Action |
|---|---|---|
| **401** | bad id token or api token | `PatReauthRequired` → `401 PAT_REAUTH_REQUIRED` |
| **400** | `expireDate` > 1 year / empty body → our bug | `PatInternalError` → `500` + log. Nothing written. |
| **429** | rate limited — init caps **10 per 60 s per client + nt** | `PatInitThrottled` → `429 PAT_INIT_RATE_LIMITED`. **No retry** — retrying would burn the same budget; the PAT is meant to be minted once and kept, then rotated via `PAT_REFRESH_API` (§5). |
| **any other status / transport / malformed 200** | transient | `PatInitUnavailable` → `503 PAT_INIT_UNAVAILABLE`. Note this also swallows permanent 4xx (a wrong `PAT_INIT_API_URL` reads as "transiently unavailable") — same behaviour as `PatRefreshClient`; the `pat.init_unexpected_status` log carries the real status. |

Init is called **only** on `POST /pat/v1/authorize` — never on the request path.
Steady state is one mint per user, then self-rotation via refresh.

**Explicit authorize always re-mints** — it does not short-circuit when the
caller already holds a valid PAT. Re-authorization is the documented remedy for
an `invalid` row (§6), so it must reach the init service rather than return the
token it is trying to replace. The cost is that a client which calls authorize
on every page load burns the 10/60 s budget and starts seeing `429`; authorize
is a one-off user action, and the request path never touches init.

## 4. Request path — `PatService.resolve(nt) → token`

```
redis hit  → local-verify
             ├─ valid    → return token
             └─ expired  → refresh
redis miss → DB row
             ├─ active + valid   → repopulate redis → return token
             ├─ active + expired → refresh
             └─ invalid / absent → raise PatReauthRequired (401 PAT_REAUTH_REQUIRED)
```

`resolve_best_effort(nt) → token | None` wraps `resolve` and swallows
`PatReauthRequired` (and any error) → used by the fail-open `/brainagent/v1`
attach so a missing/invalid PAT never breaks the existing proxy surface.

## 5. Refresh — `PatService._refresh(nt, current)`

`PUT PAT_REFRESH_API`, header `{PAT_API_HEADER_TOKEN_KEY: PAT_API_HEADER_TOKEN_VALUE}`,
body `{"patToken": current}` → response `{"patToken": new}`.

- A **per-nt redis lock** (`SET NX EX REDIS_PAT_LOCK_TTL_SECONDS`) serialises
  refresh. The lock value is a **unique owner token**; release is an atomic
  compare-and-delete, so if the lock's TTL expires mid-refresh and another
  request re-acquires it, the first holder never deletes the new owner's lock.
  A loser **polls** — sleeping between tries and returning the winner's rotated
  token as soon as it lands — instead of immediately stampeding the refresh
  service; only after the poll budget is exhausted does it self-refresh
  (double-checking the cache once more after finally acquiring the lock). The
  crash-between-rotate-and-persist race is covered by the upstream grace, so
  ragent stores only one token.
- On success: encrypt `new` → rewrite DB (`active`) + redis → return `new`.

**Error matrix** (responses from `PAT_REFRESH_API`, *not* the drive upstream):

| Status | Meaning | Action |
|---|---|---|
| **401** | not authorized / cancelled / expired / stale PAT unusable | mark DB `invalid` + evict redis → raise `PatReauthRequired` (`401 PAT_REAUTH_REQUIRED`). **No retry.** |
| **400** | body / PAT empty → our bug | raise `PatInternalError` (500) + log. PAT state untouched. |
| **429** | rate limited (transient) | exp-backoff retry ≤ `PAT_REFRESH_MAX_RETRIES` (`PAT_REFRESH_BACKOFF_SECONDS × 2^attempt`); still failing → reject *this* request (`PatRefreshExhausted`). **PAT stays `active` — never invalidated on rate-limit.** |

## 6. DB state transitions

- **User re-authorizes** → new PAT overwrites, `status='active'`, redis updated.
- **Successful refresh** → new PAT overwrites, `status='active'`, redis updated.
- **Refresh 401** → `status='invalid'`, redis cleared → user re-authorizes.

## 7. `/brainagent/v1` attach (all brain-bound calls)

**Every** ragent → brain call under `/brainagent/v1` attaches the resolved PAT
under `PAT_UPSTREAM_HEADER_NAME` (default `X-Pat-Token`), via the shared
`clients/brain_caller.py::apply_resolved_pat` helper:

- the twp-ai **run** path (`POST /brainagent/v1` → brain `/run`) — merged into
  `BrainCaller`'s extra headers, so a drive tool invoked *during* the run
  carries it;
- the **cancel** path (`POST /brainagent/v1/runs/{id}/cancel`);
- the **reverse proxy** (`/brainagent/v1/{path}` → brain `/upstream/{path}`).

(`/reconnect` and `/session/read` make no upstream call — nothing to attach.)

**Fail-open**: no PAT / invalid / redis miss / resolve error → the header is
omitted and the request is byte-for-byte what it is today. brain needs no
change; the drive/upstream side trusts the PAT and checks
`PAT[PAT_NT_KEY_NAME] == sso user`.

**A client cannot supply its own PAT.** Inbound headers only reach brain if
they are in the `BRAIN_FORWARD_HEADERS` allowlist, so by default a client-sent
`X-Pat-Token` is dropped at the edge. Even if an operator mistakenly allowlists
that name, the attach step strips any case-variant of `PAT_UPSTREAM_HEADER_NAME`
before setting the server-resolved value, so exactly one PAT header (the
server's) ever reaches brain. And a header name that collides with a
service-owned header (`X-User-Id`/`X-Brain-Key`) is refused outright.
