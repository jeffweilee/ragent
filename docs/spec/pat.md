# PAT — Personal Access Token authorization & lifecycle (T-PAT)

> Companion to `docs/00_spec.md §4.1` (endpoint) and `§4.6.9` (env). Scope: the
> PAT **service** in ragent — authorization write + on-demand resolution of a
> valid PAT. Calling an upstream *with* the PAT is the consumer's concern; the
> only consumer wired in this cycle is the `/brainagent/v1` proxy (fail-open).
>
> **Authorize takes TWO tokens.** ragent authenticates the caller with the
> **access token** the middleware already verified, while the init service wants
> an **SSO id token** as its on-behalf-of credential — different tokens with
> different audiences (`OIDC_AUDIENCE` for the id token; the resource server,
> `account` on a stock Keycloak, for the access token). The id token therefore
> arrives separately on the fixed `X-Id-Token` header. This works in **every**
> auth mode: identity comes from the mode's own scheme, the id token from its own
> header. `resolve` needs no id token at all, so the `/brainagent/v1` attach is
> unaffected.
>
> **Credentials never reach logs**: the init/refresh credential headers, the
> forwarded id token, and the attached PAT are all registered with the
> `http.upstream_error` redactor (`docs/spec/env_vars.md` §4.6.8).

## 1. Model

- **One nt ⇄ one PAT.** The PAT is a JWT the SSO service signs (12 h lifetime),
  a single self-rotating credential shared across all upstreams (the PAT maps to
  one system = our agent).
- **DB is the source of truth**, table `pat` (one row per `user_id`,
  `user_id` UNIQUE): `pat_cipher` (AES-256-GCM envelope), `status` ∈
  {`active`, `invalid`}. **Every successful refresh rewrites the row** — the DB
  always holds the *current* token, never a stale "initial" one.
- **`authorized_at` + `authorization_expires_at`** (migration 018) record the
  *current* authorization window (§9). Written by **authorize only**; `rotate`
  (refresh) leaves them alone, so a rotation never appears to extend the window.
  Nullable with no backfill — `NULL` means unknown, not "no expiry". Neither
  existing timestamp can serve: `created_at` is the first INSERT only, and
  `updated_at` is overwritten by every 12 h refresh, while the window restarts on
  every re-authorize.
- **Redis is a cache of the DB row.** Key `ragent:pat:{nt}` → encrypted PAT,
  `TTL = REDIS_PAT_TTL_SECONDS` (41 400 s = 11.5 h); `ragent:pat:lock:{nt}` is
  the refresh lock and `ragent:pat:tomb:{nt}` the revocation tombstone (§3.2).
  All redis ops are fail-soft: a redis blip degrades to a DB read, never a 500.
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
(`Depends(get_user_id)` → nt); the SSO **id token** comes from the fixed
`X-Id-Token` header. ragent mints the PAT itself via the init service; the FE
never handles a PAT. Steps:

1. **Verify the id token** with the same JWKS / issuer / audience as the access
   token (`auth/jwt.py::verify_jwt`), then require
   `id_token[RAGENT_JWT_CLAIM_USER_ID] == resolved nt`. A missing, unverifiable,
   or someone-else's id token short-circuits to `401 PAT_REAUTH_REQUIRED`
   **without calling init** — so junk can neither impersonate nor burn init's
   10/60 s budget. The reason rides `pat.authorize.rejected`; the response says
   only "re-authorization required".
2. **Mint** — `PatInitClient.init(id_token)` (§3.1), then verify the minted PAT (§2).
3. **Binding check on the minted PAT**: `PAT[PAT_NT_KEY_NAME] == resolved nt` (never trust an
   upstream-supplied identity — init is trusted to mint, not to name the owner).
   Mismatch → `401 PAT_REAUTH_REQUIRED`.
4. Encrypt → `repo.upsert(nt, cipher)` (`INSERT … ON DUPLICATE KEY UPDATE`,
   `status='active'` — re-authorization overwrites and reactivates an
   `invalid` row) → `cache.put(nt, cipher)`.

`204` on success. Failure writes nothing.

### 3.1 Init call — `PatInitClient.init(id_token) → patToken`

`POST {PAT_INIT_API_URL}` (the full mint endpoint), `content-type: application/json`, with
three headers and a date body:

| Header (name from env) | Value |
|---|---|
| `PAT_INIT_API_TOKEN_HEADER_KEY_NAME` | `PAT_INIT_API_TOKEN` (service credential) |
| `PAT_INIT_AUTHORIZE_HEADER_KEY_NAME` | the caller's verified `X-Id-Token` |
| `PAT_INIT_SSO_HEADER_KEY_NAME`       | `PAT_INIT_SSO_SITE_URL` |

Body `{"expireDate": "YYYY/MM/DD"}` — `today + PAT_INIT_EXPIRE_DAYS` (default
360, a margin under the API's **one-year ceiling**). Response `{"patToken": …}`.

**Error matrix** (responses from the init API):

| Status | Meaning | Action |
|---|---|---|
| **401** | bad id token or api token | `PatReauthRequired` → `401 PAT_REAUTH_REQUIRED` |
| **400** | `expireDate` > 1 year / empty body → our bug | `PatInternalError` → `500` + log. Nothing written. |
| **429** | rate limited — init caps **10 per 60 s per client + nt** | `PatInitThrottled` → `429 PAT_INIT_RATE_LIMITED`. **No retry** — retrying would burn the same budget; the PAT is meant to be minted once and kept, then rotated via `PAT_REFRESH_API` (§5). |
| **any other status / transport / malformed 200** | transient | `PatInitUnavailable` → `503 PAT_INIT_UNAVAILABLE`. Note this also swallows permanent 4xx (a wrong `PAT_INIT_API_URL` reads as "transiently unavailable" — which is why a path-less value is refused at boot) — same behaviour as `PatRefreshClient`; the `pat.init_unexpected_status` log carries the real status. |

Init is called **only** on `POST /pat/v1/authorize` — never on the request path.
Steady state is one mint per user, then self-rotation via refresh.

**Explicit authorize always re-mints** — it does not short-circuit when the
caller already holds a valid PAT. Re-authorization is the documented remedy for
an `invalid` row (§6), so it must reach the init service rather than return the
token it is trying to replace. The cost is that a client which calls authorize
on every page load burns the 10/60 s budget and starts seeing `429`; authorize
is a one-off user action, and the request path never touches init.

### 3.2 Revoke — `DELETE /pat/v1/authorize`

No body, and **no `X-Id-Token`**: the upstream exposes no revoke endpoint, so
nothing is called on-behalf-of the user. Identity comes from
`Depends(get_user_id)` as everywhere else. Steps, in an order where every step
is load-bearing:

1. **Tombstone first** — `cache.mark_revoked(nt)` sets `ragent:pat:tomb:{nt}`.
   From here on `PatCache.put` refuses, so a refresh already in flight cannot
   republish the rotated token after step 3.
2. **DB delete** — `repo.delete(nt)`, a hard `DELETE`. Before the eviction:
   evicting first would let a concurrent `resolve` miss the cache, read the
   still-present row and re-fill redis, a window one DB round-trip wide.
   Deleting first makes that resolve read `None` and raise instead.
3. **Evict** — clears whatever was cached before the tombstone landed.

`204` always, **including when no row existed**. The PAT is a per-caller
singleton (`uq_pat_user`), not an id-addressed object like a skill: revoking
states a target state, so repeating it is success. Whether a row existed rides
`pat.revoke.completed(existed=…)`, not the status code. `422 MISSING_USER_ID`
with no identity.

**Hard delete, not a `status='revoked'` third state** — a soft delete would keep
the ciphertext the user asked to remove, and would force `resolve`, §6 and the
revive-on-re-authorize path to change. History lives in the logs.

**Why ordering alone is not enough** (the tombstone's whole reason): `_do_refresh`
awaits between its DB write and its `cache.put`, so an entire revoke can be
scheduled in that gap on a single event loop. `rotate` returns rowcount 1 — the
§5 revoked-row check does not fire — the revoke deletes and evicts, and the
rotation then republishes the token for a row that no longer exists. Because
cache hits never consult the DB (§4), that credential would ride every brain call
for the full 11.5 h TTL. The guard therefore lives in `PatCache.put`, the single
choke point both repopulation paths (`resolve`'s re-fill, `_do_refresh`'s
rotation) go through, and runs under `WATCH`/`MULTI` so check-and-write is
atomic. Its TTL is **derived** from `PAT_REFRESH_TIMEOUT_SECONDS ×
(PAT_REFRESH_MAX_RETRIES + 1)` plus the summed backoff (~123 s at the defaults),
so retuning the refresh budget cannot shrink it below the window it covers.

The request that already rotated still returns its token — the credential is in
its memory either way, so failing it closes nothing. The next `resolve` is locked
out rather than served from a stale cache.

**The upstream PAT survives.** With no revoke endpoint, the minted PAT stays
valid upstream until its `expireDate` (§9). Once the row is deleted nobody holds
it — ragent's copy was the only one, AES-256-GCM encrypted — so the residual
record is an orphan, but it is not *revoked* upstream.

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

- **User re-authorizes** → new PAT overwrites (`upsert`), `status='active'`,
  `authorized_at` + `authorization_expires_at` restamped (§8), redis updated.
- **Successful refresh** → new PAT overwrites **via `rotate` (UPDATE-only)**,
  `status='active'`, redis updated. The window columns are **not** touched — a
  rotation must never appear to extend the authorization behind it. If the row
  is gone (revoked mid-refresh) `rotate` affects 0 rows: nothing is written, the
  cache is not published to, and the caller gets `PatReauthRequired`.
- **Refresh 401** → `status='invalid'`, redis cleared → user re-authorizes.
- **User revokes** → row **deleted**, tombstone set, redis cleared (§3.2).

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

## 8. Status — `GET /pat/v1/status`

`200` with `Cache-Control: no-store` (per-user credential state; a cached
`active` served after a revoke would lie), body:

```json
{"status": "none|active|invalid",
 "authorized_at": "2026-08-07T02:14:00Z",
 "authorization_expires_at": "2027-07-29"}
```

**A `404` means the slice is not wired** (`PAT_PUBLIC_KEY` unset → the router was
never mounted). Clients must read that as "feature off" and hide the UI, not as
an error. `422 MISSING_USER_ID` with no identity. Identity is only ever the
resolved caller — a query parameter can never read another user's state.

**Derived, not a column read.** `pat.status` is never flipped to `invalid` by a
decryption failure (key rotation / corruption), yet `resolve` raises in that
case, so returning the column would report a healthy authorization that fails on
every request. The ciphertext is decrypted and the token verified:

| DB row | API `status` |
|---|---|
| absent | `none` |
| `status != 'active'` | `invalid` |
| ciphertext undecryptable | `invalid` |
| token unverifiable (signature / `iss` / `aud`) or bound to another nt | `invalid` |
| past `authorization_expires_at` | `invalid` |
| otherwise | `active` |

`authorization_expires_at IS NULL` (rows predating migration 018) skips the
window test — unknown, not "expired".

**Expired still reports `active`.** A PAT expires every 12 h and is rotated
transparently (§2), so treating expiry as breakage would prompt a healthy
account to re-authorize twice a day. Separating "expired but refreshable" from
"structurally unusable" uses `PatTokenVerifier.verify(..., ignore_expiry=True)`,
which widens joserfc's `leeway` rather than dropping the `exp` option — joserfc
validates every time-based claim it finds, and pinning `now=0` instead would make
the `iat`/`nbf` real SSO tokens carry look like the future.

**Zero side effects — a hard constraint.** `status` must not call `resolve()`,
must not read or write redis, and must issue no outbound HTTP. Implementing it as
`resolve()` + exception mapping is the obvious shortcut and is wrong: `resolve`
refreshes an expired PAT, so a per-page-load GET would trigger refresh
round-trips, DB writes and possible `mark_invalid` — a request amplifier aimed at
the refresh service.

**The invariant is one-directional**: status must never report `active` when
`resolve` would fail. The reverse is *intended* — past the window it says
`invalid` while `resolve` can still hand out a locally-valid token, and that is
exactly how the ≤ 12 h dead-PAT window (§9) reaches the user.

## 9. Authorization window expiry (`expireDate`)

**Two expiries, only one self-heals:**

| | Period | Visible to ragent | On expiry |
|---|---|---|---|
| PAT JWT `exp` | 12 h | yes (local verify) | **auto-refreshed, user never sees it** |
| init `expireDate` | `PAT_INIT_EXPIRE_DAYS` (360, capped 364) | **no** — not in the JWT | **user must re-authorize by hand** |

Timeline:

```
day 0        authorize → PAT minted, expireDate = day 360
day 0..360   every 12 h: exp lapses → resolve → _refresh → new 12 h token (~730×)
day 360      window closes → next refresh returns 401
             → mark_invalid + evict → PatReauthRequired
             → GET /pat/v1/status reports `invalid` → user re-authorizes → +360 days
```

**This rests on refresh returning 401** (operator-confirmed). A `400` would map
to `PatInternalError` (500) and a `403`/`410` would fall into the transient
retry bucket — in both cases the row stays `active`, the user is **never**
prompted to re-authorize, and every request burns the full retry budget against
the refresh API. If the upstream ever changes that status, §5's matrix must gain
an explicit "treat as authorization gone" mapping.

**A dead PAT can look alive for up to 12 h.** An already-issued PAT stops working
the moment the window closes (operator-confirmed), but ragent's validity check is
purely local (§2) and cannot see that. So until the 12 h `exp` lapses and the
refresh 401 lands, ragent keeps attaching a PAT the upstream rejects, and the
user sees "the drive tool is broken" rather than "re-authorize".
`authorization_expires_at` closes most of this — `status` reports `invalid` as
soon as the window passes — but the authoritative signal is the upstream
rejecting the PAT, which only brain can observe.

**No proactive warning exists server-side.** Nothing scans for windows about to
close (the reconciler has no PAT arm), and re-authorization needs the user's id
token so it can never be automated. The window is surfaced, not enforced:
`authorization_expires_at` is a **prediction** — the upstream stays authoritative
and refresh-401 remains the only thing that invalidates a PAT — so it may be read
by `status`, dashboards and alerts, and **never** by `resolve`. A window the
upstream silently extended would otherwise be cut short.

**Cohort effect worth planning for**: everyone onboarded in the same week has
their window close in the same week a year later. `pat_refresh_total{outcome=
"unauthorized"}` (or the equivalent query over `authorization_expires_at`) is how
that becomes visible before the support queue does.

## 10. Known limitations

- **Revoke does not reach the upstream** (§3.2): no revoke endpoint exists, so
  the minted PAT lives out its `expireDate` upstream as an orphan nobody holds.
- **Redis down during a revoke** — `PatCache` is fail-soft by design, so neither
  the tombstone nor the eviction lands and a cached PAT survives to its TTL. The
  DB row is still deleted, so this is bounded at 11.5 h and self-heals, but it is
  the one case where revocation is not immediate.
- **Re-authorizing kills the previous PAT immediately** (operator-confirmed), so
  a request holding the old token in memory fails once. ragent stores exactly one
  PAT and overwrites it, so no ragent-side handling is needed.
- **`authorization_expires_at` is date-granular** and evaluated by the upstream
  in its own timezone, so the real moment of expiry is fuzzy by up to a day.
  Never gate on it.
