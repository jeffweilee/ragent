# PAT — Personal Access Token authorization & lifecycle (T-PAT)

> Companion to `docs/00_spec.md §4.1` (endpoint) and `§4.6.9` (env). Scope: the
> PAT **service** in ragent — authorization write + on-demand resolution of a
> valid PAT. Calling an upstream *with* the PAT is the consumer's concern; the
> only consumer wired in this cycle is the `/brainagent/v1` proxy (fail-open).

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

SSO identity comes from the request header (`Depends(get_user_id)` → nt); the
PAT is supplied in the body `{patToken}` (temporary — swapped for the fetch-PAT
API when it lands). Steps:

1. Verify the PAT (§2).
2. **Binding check**: `PAT[PAT_NT_KEY_NAME] == resolved nt` (never trust a
   body-supplied identity). Mismatch → `401 PAT_REAUTH_REQUIRED`.
3. Encrypt → `repo.upsert(nt, cipher)` (`INSERT … ON DUPLICATE KEY UPDATE`,
   `status='active'` — re-authorization overwrites and reactivates an
   `invalid` row) → `cache.put(nt, cipher)`.

`204` on success. Failure writes nothing.

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

- A **per-nt redis lock** (`set nx ex REDIS_PAT_LOCK_TTL_SECONDS`) serialises
  refresh; a loser re-reads the cache (the winner may have written the new
  token) before falling back. This prevents thundering-herd + rotation clobber;
  the crash-between-rotate-and-persist race is covered by the upstream grace, so
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

## 7. `/brainagent/v1` attach (this cycle's only consumer)

The proxy calls `resolve_best_effort(user_id)` and, when a token comes back,
attaches it under `PAT_UPSTREAM_HEADER_NAME` (default `X-Pat-Token`) to the
brain upstream request. **Fail-open**: no PAT / invalid / redis miss → the
header is omitted and the request is byte-for-byte what it is today. brain needs
no change; the drive/upstream side trusts the PAT and checks
`PAT[PAT_NT_KEY_NAME] == sso user`.
