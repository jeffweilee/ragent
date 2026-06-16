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
- `humanInTheLoopMeta.isInterrupt=true` → standalone `TEXT_MESSAGE` carrying
  `interruptMessage` as the delta.
- `[Done]` sentinel → `RUN_FINISHED`.
- **No `<hidden>` stripping on the stream:** the SSE deltas are the upstream
  agent's own generated output (assistant text / reasoning / tool); the
  `<hidden>` context/state preamble exists only on the user turn sent upstream
  and is never echoed back into the response stream, so there is nothing to strip
  here. Hidden stripping applies only to the session-history read (§3.4.8 below).

**Error contract (breaking change vs v2):** every failure — rate-limit, upstream
`returnCode != 96200`, 5xx, and timeout — is emitted as a single `RUN_ERROR`
event over a `200 text/event-stream` response, with `code` set to
`CHATAGENT_RATE_LIMITED` / `CHATAGENT_UPSTREAM_ERROR` / `CHATAGENT_TIMEOUT`. v3
never returns an HTTP `429`/`502`/`504` (v2 does).

---

## §3.4.8 `/chatagent/v3` session management — twp-ai-shaped history

`/chatagent/v3` also exposes the session-management surface (each route registered
only when its upstream URL env var is set), proxying the **same** upstream as
`/chatagent/v1/session*` but returning the persisted history in the **twp-ai
message shape**. This is the reason the session interface is versioned up: the
message shape changes, while the upstream wire contract is untouched.

- `GET /chatagent/v3/sessionList` — each entry's `sessionName` has the
  machine-context wrapper stripped (the upstream derives the title from the first
  user turn, which carries the block); other metadata is passed through.
- `GET /chatagent/v3/session?session=<id>` — the upstream session envelope
  (`session`, …) is preserved, `sessionName` is stripped (same reason as
  sessionList), and every `messages[]` entry is reshaped to
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

These are JSON proxy routes (not the SSE stream), so timeout / upstream failures
map to HTTP `504` / `502` as in v1 — the v3 `RUN_ERROR` framing applies only to
`POST /chatagent/v3`.
