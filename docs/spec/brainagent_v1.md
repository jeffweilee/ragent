# brainagent_v1 — twp-ai Protocol over the ragent-brain Upstream

> Part of [docs/00_spec.md §3.4](../00_spec.md#34-chat-pipeline). Standard: [docs/00_rule.md](../00_rule.md).

---

## Purpose & relationship to `/chatagent/v3`

`/brainagent/v1` is a **new, independent** chat + management surface that fronts
the **ragent-brain** service (`brain.app`, twp-ai native over `POST /run` plus a
`/upstream/*` management API). It reuses the `/chatagent/v3` edge machinery
(rate limit, resumable Redis-stream buffer, reconnect, `RUN_ERROR` framing) but
differs from v3 in two deliberate ways:

1. **Upstream speaks twp-ai natively.** brain's `/run` already emits the full
   twp-ai SSE lifecycle (`RUN_STARTED` → `TEXT_/REASONING_/TOOL_/STATE_*` →
   `RUN_FINISHED{success|interrupt}` / `RUN_ERROR`). Unlike the ADK upstream
   (which emits the `returnData.messages[]` wire that `ADKCaller`/`ADKAgent`
   must **translate**), brain needs **no translation** — the ragent side is a
   **passthrough** relay.
2. **Thin edge (option A).** ragent does **not** inject skills, does **not**
   resolve attachments, and does **not** build a `<hidden>` context/state
   preamble. brain owns skills / memory / projects / attachments; ragent passes
   `messages`, `context`, `state`, `forwardedProps`, `resume`, `attachment_ids`,
   and `model` through unchanged. ragent's responsibilities are strictly edge:
   auth, rate limit, resumable streaming, and transport-error framing.

Registered only when `BRAIN_API_URL` is set. **brain requires zero code change**
to be fronted by this surface — only deployment + env.

---

## §3.4.10 `POST /brainagent/v1` — twp-ai run over ragent-brain

Request: `RunAgentInput` (§ twp-ai schema). Response: `text/event-stream` of
twp-ai SSE events. Auth: `get_user_id` (resolved caller).

It reuses the twp-ai `Agent`/caller abstraction: a **`BrainAgent`**
(`packages/twp-ai/src/twp_ai/agents/brain.py`) satisfies the `Agent` protocol
and delegates transport to a **`BrainCaller`** protocol
(`packages/twp-ai/src/twp_ai/callers/brain.py`); the concrete HTTP client lives
ragent-side in `src/ragent/clients/brain_caller.py`.

**Request → upstream conversion (passthrough):**
- The POST body is forwarded to brain `POST {BRAIN_API_URL}/run` **as-is** — the
  full `RunAgentInput` JSON (`messages`, `context`, `state`, `tools`,
  `forwardedProps`, `resume`, `attachment_ids`, `model`). brain's engine reads
  the structured `context`/`state` fields directly, so **no `<hidden>` preamble
  is built** (contrast §3.4.7, which folds context/state into a single upstream
  `inputData.message` because the ADK wire has only one text field).
- **Headers:** `X-User-Id: {resolved user}` and `X-Brain-Key: {BRAIN_KEY}`
  (service-to-service). The user's JWT is **not** forwarded to brain — brain
  authenticates the caller by `X-Brain-Key` and scopes data by `X-User-Id`.
- **Session id ownership (Model B, unchanged):** request `thread_id` is optional;
  when the client omits it ragent mints one (`new_id()`) and sends it in the
  body. brain echoes it verbatim in `RUN_STARTED.thread_id` (brain's engine uses
  `body.thread_id`), so the minted id round-trips without a rewrite.
- `model` **is** forwarded (brain honours `body.model`), unlike v3 which drops it.
- **Human-in-the-loop resume** is forwarded verbatim: brain's `RunAgentInput`
  carries `resume: Array<{interruptId, status, payload?}>` natively and its
  engine resolves the interrupt. ragent does **not** re-interpret resume (v3's
  `>1 resolved → RUN_ERROR` rule is an ADK-wire constraint that does not apply).

**Upstream → response conversion (passthrough):**
- brain's SSE frames are relayed **unchanged**. `BrainAgent` does **not** re-emit
  its own `RUN_STARTED` / `RUN_FINISHED` — brain already brackets the run, and a
  second envelope would duplicate the lifecycle. Every event brain emits
  (including `STATE_SNAPSHOT`, `MESSAGE_CARD`, and any twp-ai extension carried
  by `extra="allow"`) passes straight through.
- **`BrainAgent` owns exactly one synthesized event: `RUN_ERROR`**, and only for
  a **transport-level** failure — brain unreachable, connection reset, or timeout
  **before any frame arrived**. Once brain's stream has started, brain emits its
  own terminal frame (`RUN_FINISHED` or `RUN_ERROR`) and ragent relays it. A
  transport failure mid-stream (after ≥1 frame) also closes with a synthesized
  `RUN_ERROR` so the buffer always terminates.

**Error contract.** Matches v3's shape — every failure is a single `RUN_ERROR`
over a `200 text/event-stream`, never an HTTP `4xx`/`5xx`. Codes:
- `BRAINAGENT_RATE_LIMITED` — ragent rate limiter tripped (no upstream call).
- `BRAINAGENT_TIMEOUT` — transport timeout to brain.
- `BRAINAGENT_UPSTREAM_ERROR` — brain unreachable / connection error / non-2xx
  before the stream started.
- brain-originated `RUN_ERROR` frames (engine failures) are relayed with brain's
  own `code`/`message` untouched.

**Resumable stream.** Reuses the **same** `ChatStreamStore` machinery as
§3.4.7 verbatim — the background producer tees each relayed twp-ai frame into
`chatstream:{user}:{thread}:{stream_id}`, the current-run pointer and stashed
user turn are recorded, and `GET /brainagent/v1/reconnect?thread_id` replays the
buffer (emitting a reconstructed `USER_MESSAGE` on a from-start replay). All
`CHATAGENT_STREAM_*` semantics, TTLs, and the legacy-fallback-on-Redis-outage
behaviour carry over unchanged. No store wired → legacy connection-bound stream.

### `POST /brainagent/v1/runs/{run_id}/cancel`

Cooperative cancel. Proxies to brain `POST /runs/{run_id}/cancel` with
`X-User-Id` (owner-scoped) + `X-Brain-Key`. Returns brain's `{cancelled: bool}`
(`200`) or `404` verbatim. Not part of the `/upstream/*` proxy family (different
path + header shape).

---

## §3.4.11 `/brainagent/v1/*` — brain management surface (authenticated reverse proxy)

Every brain `/upstream/*` route is fronted by a **single generic authenticated
reverse proxy** so the entire brain management surface is reachable through
ragent and new brain `/upstream/*` routes are covered automatically. ragent path
`/brainagent/v1/{path}` maps to brain `{BRAIN_API_URL}/upstream/{path}`.

**Proxy contract:**
1. **Auth.** `get_user_id` resolves the caller from the JWT; the proxy attaches
   `X-Brain-Key: {BRAIN_KEY}` server-to-server.
2. **User override (security-critical).** The resolved `user_id` is written into
   the outbound request as `user`, **overriding any client-supplied value**, in
   **both** places brain reads it: the query string `?user=` **and** (when the
   body is a JSON object) the body `"user"` field. The client's `user` is never
   trusted — a caller cannot read or mutate another user's data by forging it.
3. **Method / path / query** are forwarded unchanged (minus the overridden `user`).
4. **Request body.** JSON bodies are forwarded after the `user` override;
   base64-carrying JSON (artifact upload) is ordinary JSON.
5. **Response.** Status + body are relayed. brain's `422 {"error": code,
   "params": {...}}` i18n envelope is passed through verbatim (the frontend
   depends on the stable `code`). **Binary responses** (artifact download,
   `GET /upstream/artifacts/{id}`) relay the raw bytes with `Content-Type` and
   `Content-Disposition` preserved — not JSON-decoded.
6. **Transport errors.** timeout → `504`, unreachable / connection error →
   `502` (`application/problem+json`), matching the `_chatagent_proxy` JSON
   convention. (The `RUN_ERROR` SSE framing applies **only** to `POST
   /brainagent/v1`, never to these JSON routes.)

**Covered routes** (brain `/upstream/{x}` → ragent `/brainagent/v1/{x}`), all via
the generic proxy:

| Family | brain routes (relative to `/upstream`) |
|---|---|
| session | `GET /session`, `GET /sessionList`, `PUT /session`, `DELETE /session` |
| memory | `GET /memory`, `PUT /memory/core`, `POST /memory/archival`, `DELETE /memory/archival/{mem_id}` |
| projects | `GET /projects`, `POST /projects`, `PUT /projects/{id}`, `DELETE /projects/{id}` |
| sources | `GET /projects/{id}/sources`, `POST /projects/{id}/sources`, `DELETE /projects/{id}/sources/{doc_id}` |
| artifacts | `GET /artifacts`, `POST /artifacts`, `GET /artifacts/{id}` (binary), `DELETE /artifacts/{id}` |
| skills | `GET /skills`, `POST /skills`, `PUT /skills/{id}`, `PUT /skills/{id}/enabled`, `DELETE /skills/{id}` |
| preferences | `GET /preferences/candidates`, `POST /preferences/candidates/{id}` |
| schedules | `GET /schedules`, `POST /schedules`, `PUT /schedules/{id}`, `PUT /schedules/{id}/enabled`, `DELETE /schedules/{id}`, `GET /schedules/{id}/runs`, `POST /schedules/{id}/run` |

### Routes deliberately **not** fronted by the user proxy

- `GET /healthz` — infra probe; ragent has its own `/livez`/`/readyz`. brain
  health may be folded into ragent readiness later, not exposed as a user route.
- `POST /upstream/reindex` — server-to-server **admin** action (requires brain's
  `X-Brain-Admin-Key`, rebuilds ES from MariaDB). Exposed — if at all — as a
  ragent **admin** route behind the existing admin guard, never on the
  user-authenticated proxy. **Deferred** unless ops needs it.
- `GET /agent/card`, `GET /.well-known/agent-card.json`, `POST /a2a` — the
  **A2A** (agent-to-agent) plane. A different trust boundary: peer agents call
  brain directly with their own `x-api-key`. Not a ragent-user function, so not
  proxied. Making ragent an A2A front door is a separate design, out of scope.

---

## Composition & wiring

- **Env** (read only in `bootstrap/composition.py`):
  - `BRAIN_API_URL` — brain base URL (e.g. `http://brain:8100`). Unset → the
    whole `/brainagent/v1` surface is not registered.
  - `BRAIN_KEY` — `X-Brain-Key` service-to-service secret (never logged).
  - `BRAIN_TIMEOUT_SECONDS` — transport timeout (default 30).
- **Agent factory.** `_build_brain_agent_factory(http, brain_url, brain_key,
  timeout)` returns `factory(user_id) -> Agent` building `BrainCaller` wrapped in
  `BrainAgent` (per-request, like the ADK factory). composition is the **only**
  file that names the concrete `BrainCaller`/`BrainAgent` (DIP seam); the router
  receives an opaque `agent_factory`.
- **Container** gains `brain_api_url`, `brain_key`, `brain_agent_factory`, and a
  `brain_proxy` helper (or reuses `http` + a small proxy service). The resumable
  store, rate limiter, and NATS publisher are the **same singletons** v3 uses.
- **Registration** (`bootstrap/app.py`): mount `create_brainagent_v1_router(...)`
  and the generic proxy router only when `brain_api_url` is set.

**Deployment (brain, zero code change).** brain runs as its own service; point
its datastores at ragent's infrastructure by **config** (shared MariaDB server /
ES cluster, distinct database name / index prefix) — not by merging code. brain
env: `BRAIN_KEY`, `BRAIN_DB_DSN`, `BRAIN_ES_URL`, `BRAIN_LLM_BASE_URL/_API_KEY/_MODEL`,
`BRAIN_EMBED_MODEL`, `BRAIN_ARTIFACTS_S3_*` (+ optional `SCHED_*`, `LANGFUSE_*`).
