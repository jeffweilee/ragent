# chatagent_v3 — twp-ai Protocol over ChatAgent Upstream

> Part of [docs/00_spec.md §3.4](../00_spec.md#34-chat-pipeline). Standard: [docs/00_rule.md](../00_rule.md).

---

## §3.4.7 `POST /chatagent/v3` — twp-ai protocol over the ChatAgent upstream

`/chatagent/v3` exposes the **twp-ai protocol wire contract** (request:
`RunAgentInput`; response: twp-ai SSE event stream) while proxying to the same
external ChatAgent service as `/chatagent/v2` (shares `CHATAGENT_API_URL`,
`CHATAGENT_AUTH`, the `chatagent:{user_id}` rate limit, and
`CHATAGENT_TIMEOUT_SECONDS`). It is a **distinct service** from `/twp/v1/run`
(which is a native agent host backed by the internal LLM); the two are unrelated
and may diverge freely. Registered only when `CHATAGENT_API_URL` is set.

It reuses the twp-ai `Agent`/caller abstraction: an `ADKAgent`
(`packages/twp-ai/src/twp_ai/agents/adk.py`) owns the event flow and delegates
transport to an `ADKCaller` protocol; the concrete proxy lives ragent-side in
`src/ragent/clients/adk_caller.py`.

**Request → upstream conversion:**
- The upstream is a general, tool-capable agent that owns its own persona and
  keeps conversation memory by `session`, so v3 imposes **no** assistant
  persona and does **not** enumerate tools. It only surfaces the client-supplied
  `context`/`state` that the single-field wire would otherwise drop: a
  `<hidden>` preamble wrapping `<context>{json}</context>` and/or
  `<state>{json}</state>` is prepended to the last `role="user"` message content,
  and the combined text becomes upstream `inputData.message`. The `<hidden>`
  block is a short-term fix so the frontend can strip the machine-supplied
  context/state from the rendered agent history (the upstream agent's system
  prompt is configured to read the block); a tag is emitted only for the field
  that is present. With no `context` and no `state` the message is the bare
  user text (a plain pass-through); conversely, when a preamble exists but there
  is no `role="user"` message content, the message is the bare `<hidden>` block
  (no trailing separator). Wrapper tokens (`<hidden>`/`<context>`/`<state>`, their
  closing forms, and whitespace/attribute variants a lenient stripper would honour
  — e.g. `</hidden >`, `<hidden attr="1">`) appearing **inside** the serialized
  `context`/`state` payload are neutralized (`<` → `&lt;`, `>` → `&gt;`) so a
  hostile value cannot close the block early and leak into the visible history.
  Concretely:

  ```
  <hidden>
  <context>[{"description": "current page", "value": "checkout"}]</context>
  <state>{"draft": "v1"}</state>
  </hidden>

  {last user message}
  ```
- `metadata` is server-injected: `apName` (= `CHATAGENT_AP_NAME`), `user`
  (resolved caller), `userToken` (raw JWT header), and `session = threadId`.
- **Session id ownership (Model B):** request `threadId` is **optional**. ragent
  owns the session id — when the client omits it (a brand-new conversation),
  ragent mints one (`new_id()`) and uses it as the upstream `session`, so the
  upstream always receives ours and never mints its own. The resolved id is
  echoed in `RUN_STARTED.threadId`; the client reuses it on every later turn.
  Request `messages[].id` is the client's optimistic id — ignored by the proxy;
  the upstream assigns the authoritative `messageId` returned in the stream.
- `stream` is always `true`. `model` is **not** forwarded (the upstream decides,
  matching v2).
- `tools`/`forwardedProps` are accepted but not forwarded; client tool-call
  continuation is not yet implemented.
- **Human-in-the-loop resume:** request `resume` is optional —
  `Array<{interruptId, status: "resolved" | "cancelled", payload?}>` answering a
  prior `RUN_FINISHED` interrupt. When present, the turn answers the interrupt
  instead of sending a new user message:
  - `status="resolved"` → upstream `inputData` becomes
    `{"lastMessageId": interruptId, "message": ""}` (the upstream only supports
    go / no-go, so `payload` is accepted but **not** forwarded). The composed
    user message / `<hidden>` preamble is **not** sent on a resume turn.
  - `status="cancelled"` → no upstream call is made; the run finishes with a
    `success` outcome and an empty body.
  - The upstream takes a single `lastMessageId`, so **more than one `resolved`**
    interrupt in one request is rejected as a `RUN_ERROR`
    (`CHATAGENT_INVALID_RESUME`). One `resolved` alongside any number of
    `cancelled` entries is fine.

**Upstream → response conversion:**
- Each SSE line is `data: {json}\n\n`; the stream terminates with `data: [Done]`.
- `returnData.messages[].content` → `TEXT_MESSAGE_CONTENT` (bracketed by
  `TEXT_MESSAGE_START`/`TEXT_MESSAGE_END`; `messageId` taken from upstream
  `messages[].messageId`, one block per distinct id).
- `messageMeta.langgraph_node` (`planner`/`commander`/`summarizer`) → each node
  gets its own block (keyed by its upstream `messageId`). The `planner` node is
  the agent's plan/reasoning step and is surfaced as a reasoning block
  (`REASONING_START` → `REASONING_MESSAGE_START` / `REASONING_MESSAGE_CONTENT`* /
  `REASONING_MESSAGE_END` → `REASONING_END`) instead of a `TEXT_MESSAGE` block;
  every other node produces a `TEXT_MESSAGE` block.
- `finish_reason="tool_calls"` + `tool_calls` → `TOOL_CALL_START` / `TOOL_CALL_ARGS`
  / `TOOL_CALL_END` events; the upstream's tool-result turn (`role="tool"`) →
  `TOOL_CALL_RESULT`.
- `humanInTheLoopMeta.isInterrupt=true` → the run pauses. The interrupt does
  **not** get its own block; instead it is collected into `RUN_FINISHED.outcome`
  (below). The interrupt message's own `content` / `tool_calls` still stream
  normally (so the frontend can render the pending tool call it must approve).
- `[Done]` sentinel → `RUN_FINISHED`. Every v3 `RUN_FINISHED` carries an
  `outcome` (a breaking add vs the native `/twp/v1` agents, which omit it):
  - `outcome = {"type": "success"}` when the turn produced no interrupt.
  - `outcome = {"type": "interrupt", "interrupts": [Interrupt, …]}` when one or
    more upstream messages flagged `isInterrupt`. Each `Interrupt` is
    `{id, reason, message?, toolCallId?, metadata?}`:
    - `id` = upstream `messageId` (echoed back as the resume `interruptId`).
    - `reason` = upstream `finish_reason`, or `"interrupt"` when the turn carries
      no tool call.
    - `message` = `humanInTheLoopMeta.interruptMessage` (omitted when absent).
    - `toolCallId` = `tool_calls[0].id` (omitted when the turn has no tool call).
    - `metadata` = the raw `displayMeta` object (omitted when absent).
    - `responseSchema` / `expiresAt` are reserved in the wire type but not
      populated — the upstream does not yet emit them.
- **No `<hidden>` stripping on the stream:** the SSE deltas are the upstream
  agent's own generated output (assistant text / reasoning / tool); the
  `<hidden>` context/state preamble exists only on the user turn sent upstream
  and is never echoed back into the response stream, so there is nothing to strip
  here. Hidden stripping applies only to the session-history read (§3.4.8 below).

**Error contract (breaking change vs v2):** every failure — rate-limit, upstream
`returnCode != 96200`, 5xx, timeout, and an invalid `resume` (>1 resolved) — is
emitted as a single `RUN_ERROR` event over a `200 text/event-stream` response,
with `code` set to `CHATAGENT_RATE_LIMITED` / `CHATAGENT_UPSTREAM_ERROR` /
`CHATAGENT_TIMEOUT` / `CHATAGENT_INVALID_RESUME`. v3 never returns an HTTP
`429`/`502`/`504` (v2 does).

**Resumable stream (Redis Stream buffer).** When a `ChatStreamStore` is wired
(`CHATAGENT_API_URL` set + Redis reachable), the POST stream is **decoupled from
the client connection** so a refresh/disconnect does not abort generation:

- On POST, a single background **producer** tees every twp-ai SSE frame into the
  Redis Stream `chatstream:{user}:{thread}:{stream_id}` via `XADD`, then writes an
  `eos` sentinel and sets the TTL. The `stream_id` is **server-minted per POST**
  (`new_id()`), never the client `run_id`: v3 never deduplicated on `run_id`, so a
  repeated `run_id` must still reach upstream and produce a fresh run, not silently
  replay the previous buffer. (The `SET NX` lock on `…:{stream_id}:lock` therefore
  never collides; it only marks the startup window for reconnect and detects a
  Redis outage → legacy fallback.) `ADKAgent.run` never raises, so the buffer
  always closes with a terminal `RUN_FINISHED`/`RUN_ERROR` frame. The POST also
  records two recovery aids: a per-thread **current-run pointer**
  `chatcurrent:{user}:{thread} = stream_id` (so reconnect resolves the run
  server-side without a client id) and the run's **user turn** at
  `…:{stream_id}:user` (the live stream carries only the assistant side, so the
  question must be stashed to be replayable).
- The POST response and `GET /reconnect` are **consumers**: they replay the
  buffer with `XRANGE` (polling, not blocking — one cursor loop serves both the
  live stream and a cross-replica reconnect, since the producer may run on a
  different pod) and attach each Redis entry id as the SSE `id:` line. They stop
  at the `eos` sentinel, or after `CHATAGENT_STREAM_IDLE_TIMEOUT_SECONDS` of no
  progress (a producer that died without closing).
- `GET /chatagent/v3/reconnect?thread_id` — **resolves the thread's current run
  server-side** from the `chatcurrent:` pointer; it deliberately does **not**
  accept a client `run_id`, which can be stale (another tab/device started a
  newer run) and would resurrect an old, already-persisted turn. On a from-start
  replay it first emits a `USER_MESSAGE` event reconstructed from the stashed
  user turn (so a client that lost local state on refresh recovers the question
  from the server, not from possibly-stale storage), then replays the buffer.
  Header `Last-Event-ID` is the **exclusive** resume cursor (the last entry the
  client saw); omit it to replay from the start (an incremental resume does not
  re-emit the user turn). A malformed `Last-Event-ID` (not a `<ms>-<seq>` entry
  id) is rejected up front — it would otherwise make the XRANGE cursor raise — as
  a `RUN_ERROR(CHATAGENT_STREAM_EXPIRED)`, never a 500. The pointer is per-user,
  so another user resolves no run; a thread with no current run, or one whose
  buffer has aged out past the TTL, yields the same
  `RUN_ERROR(CHATAGENT_STREAM_EXPIRED)`, the client's cue to fall back to
  `GET /chatagent/v3/session` for the completed history (§3.4.8). A run whose
  producer holds the start lock but has not written its first frame yet (startup
  race) is still treated as reconnectable.
  - **Only a still-running run is replayed.** Once the run finishes (the `eos`
    sentinel is the buffer's last entry) reconnect returns
    `RUN_ERROR(CHATAGENT_STREAM_EXPIRED)` even though the buffer still lingers for
    the live consumer to drain. The upstream persists the turn right after the
    stream ends, so a finished run is (essentially) already in `GET /session`;
    refusing it here means the client takes that turn from session and there is no
    buffer/session overlap to de-duplicate. The only cost is a brief window — the
    upstream write latency — where a just-finished turn is in neither surface; the
    next reload picks it up from session.
- Buffer retention is bounded by `REDIS_STREAM_TTL_SECONDS` (default 300s) and
  `REDIS_STREAM_MAXLEN` (default 10000, approximate trim). With no store wired —
  **or when the stream Redis is unreachable** (`try_start` cannot acquire the
  lock) — v3 falls back to the legacy connection-bound stream (correct, not
  resumable) so chat keeps working; a transient Redis blip mid-stream ends the
  consumer via its idle timeout rather than crashing it.

---

## §3.4.8 `/chatagent/v3` session management — twp-ai-shaped history

`/chatagent/v3` also exposes the session-management surface (each route registered
only when its upstream URL env var is set), proxying the **same** upstream as
`/chatagent/v1/session*` but returning the persisted history in the **twp-ai
message shape**. This is the reason the session interface is versioned up: the
message shape changes, while the upstream wire contract is untouched.

- `GET /chatagent/v3/sessionList` — each entry's `sessionName` has the
  machine-context wrapper stripped (the upstream derives the title from the first
  user turn, which carries the block); other metadata is passed through. When the
  stream store is wired, each entry is also enriched with two live-status booleans
  (the session id **is** the run `thread_id`):
  - `running` — the thread's current run is still streaming (run pointer set, no
    `eos`); drives the list spinner. Derived from the existing run pointer + buffer,
    so no extra bookkeeping.
  - `hasNewReply` — a run finished for this thread and the user has not read it
    since; drives the new-reply dot. Backed by a per-`(user, thread)` Redis flag
    (`chatunread:`) set when a run completes and dropped when the user **reads** it —
    either by opening the session (`GET /session`, after a successful fetch) **or** by
    watching the live run to its `eos` (the active POST/`reconnect` consumer clears on
    drain; a client that disconnects before `eos` leaves the dot, so a backgrounded
    run that finishes unwatched stays unread). `REDIS_UNREAD_TTL_SECONDS` (default 30d).
    It is a presence flag, not a timestamp — `hasNewReply` is a plain `EXISTS`.
  - With no store wired the list degrades to title-only (the fields are omitted).
- `GET /chatagent/v3/session?session=<id>` — the upstream session envelope
  (`session`, …) is preserved, `sessionName` is stripped (same reason as
  sessionList), **human-in-the-loop interrupt turns (`humanInTheLoopMeta.isInterrupt=true`)
  are dropped entirely** (they are transient approval prompts surfaced live via
  `RUN_FINISHED.outcome`, §3.4.7 — not conversation messages, so they must not
  render in history; this keeps the read consistent with the stream), and every
  remaining `messages[]` entry is reshaped to
  `{id, role, content, createTime, updateTime}`:
  - `createTime` / `updateTime` are the upstream persistence timestamps passed
    through verbatim (null when the upstream omits them).
  - `role` is derived from the upstream role + `messageMeta.langgraph_node` by the
    **same `node_to_role` rule as the v3 stream** (§3.4.7): `user`→`user`,
    `tool`→`tool`, assistant+`planner`→`reasoning`, every other assistant
    node→`assistant`.
  - `content` has the machine-context wrapper stripped — the persisted user turn
    carries the preamble the frontend prepended, and it must not surface in
    rendered history. Both forms are removed (whitespace / attribute tag variants
    included): the current `<hidden>…</hidden>` block **and** the legacy bare
    `<context>…</context>` block that sessions created before v3 carry (backward
    compatibility). This is the **only** place the strip applies (the stream,
    §3.4.7, never carries the block).
- `PUT` / `DELETE /chatagent/v3/session` — proxied unchanged (rename / delete; no
  message bodies).
**Realtime status over NATS (not an HTTP route).** Instead of an SSE endpoint, ragent
publishes live status transitions to a per-user NATS subject `<NATS_SESSION_SUBJECT_PREFIX>.<user_id>.status`
(default `session.<user_id>.status`); the frontend subscribes over its **own already-open**
NATS connection and merges the delta onto its `sessionList` snapshot. This keeps the
delta off ragent's HTTP/threadpool path entirely and uses NATS's native cross-pod
fan-out (any API replica's producer reaches every subscriber). Payloads mirror the
list fields:

- `{session, running:true}` when a run starts,
- `{session, running:false, hasNewReply:true}` when it finishes,
- `{session, hasNewReply:false}` when the user reads the session (`GET /session`, or the
  active consumer draining to `eos`).

Publishing is **best-effort / fire-and-forget** (`run_coroutine_threadsafe` from the
producer thread); a publish failure costs only one live nudge. NATS is unconfigured
(`NATS_SERVERS` unset) → no realtime push, list stays snapshot-only.

- **Snapshot + delta (lossy):** NATS core pub/sub is at-most-once, so the delta is a
  *hint*, never a reliable event log. The durable truth is the `sessionList` snapshot
  (`running`/`hasNewReply` above). The client **must** take the `sessionList` snapshot
  on mount and **re-fetch it to re-sync** on NATS (re)connect / error (and may poll it
  periodically as a backstop). A run the client is itself streaming already updates from
  that session's chat stream, so the channel mainly carries cross-tab / background
  transitions a snapshot would otherwise miss; the active session's own dot is cleared
  server-side on `eos` drain, so a client need not special-case it.

`sessionList` / `session` (GET/PUT/DELETE) are JSON proxy routes; timeout / upstream
failures map to HTTP `504` / `502` as in v1 — the v3 `RUN_ERROR` framing applies only to
`POST /chatagent/v3`. The NATS publish never affects an HTTP response (fail-soft).
