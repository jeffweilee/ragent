# Chatagent Agent Backend — DIP/OCP abstraction & brain-swap runbook

> Authored: 2026-06-23  
> Maintained by: Dev  
> Source modules: `src/ragent/routers/chatagent_v3.py`, `src/ragent/bootstrap/composition.py`,
> `src/ragent/clients/adk_caller.py`, `src/ragent/services/chatagent_session.py`,
> `packages/twp-ai/src/twp_ai/agent.py`, `packages/twp-ai/src/twp_ai/agents/`

This document records why `/chatagent/v3` is structured the way it is, and how
to swap the upstream "agent brain" for a different implementation (library-based
or API-based) without touching router code.

---

## Table of Contents

1. [Motivation & SOLID rationale](#1-motivation--solid-rationale)
2. [The `Agent` Protocol](#2-the-agent-protocol)
3. [Current implementations](#3-current-implementations)
4. [Call chain](#4-call-chain)
5. [Brain-swap runbook](#5-brain-swap-runbook)
6. [Zero-modification / two-touch-files checklist](#6-zero-modification--two-touch-files-checklist)
7. [Known limitation: session history is not portable](#7-known-limitation-session-history-is-not-portable)

---

## 1. Motivation & SOLID rationale

`/chatagent/v3` proxies a chat run to an external "ADK"-style upstream agent
service. Before this refactor, the router (`routers/chatagent_v3.py`) directly
imported and inline-constructed `ADKAgent`/`ADKCaller` — a high-level module
(the router, which owns HTTP contract / rate-limit / resumable-stream policy)
depending on low-level concrete implementations. That is the dependency
direction DIP forbids, and it meant swapping the upstream brain required
editing the router itself (an OCP violation).

The fix did not require inventing new abstractions: `packages/twp-ai` already
ships a generic `Agent` Protocol and uses it correctly at `/twp/v1`
(`DirectLLMAgent(RagentCaller(...))` injected into `twp_ai.app.create_router`).
`/chatagent/v3` needed the same pattern, adapted for one constraint: the ADK
caller carries per-request state (`user_id`, `user_token`), so it cannot be a
singleton `Agent` instance like `/twp/v1`'s. The router now receives an
**`AgentFactory`** — a `(user_id, user_token) -> Agent` callable — built once
in the composition root and called per request.

| Principle | How it's satisfied |
|---|---|
| **S** | Router: HTTP contract / rate-limit / resumable-stream. Composition root: backend selection & wiring. Caller: wire-format translation. Each has one reason to change. |
| **O** | A new backend = a new `Agent` implementation + a new `_build_xxx_agent_factory()` branch in `composition.py`. Zero edits to `routers/chatagent_v3.py`. |
| **L** | Any object satisfying `Agent.run(request, model) -> Generator[str, None, None]` can replace the factory's return value with no router changes — proven by `/twp/v1` (`DirectLLMAgent`) and `/chatagent/v3` (`ADKAgent`) already coexisting. |
| **I** | `Agent` has exactly one method (`run`); so does the `ADKCaller`/`LLMCaller` Protocol it wraps. |
| **D** | The router depends on `Agent` (abstract); concrete classes depend on the same abstraction and are wired together only in the composition root. |

---

## 2. The `Agent` Protocol

Defined once, in `packages/twp-ai/src/twp_ai/agent.py`:

```python
class Agent(Protocol):
    def run(
        self,
        request: RunAgentInput,
        model: str,
    ) -> Generator[str, None, None]: ...
```

`routers/chatagent_v3.py` defines a local type alias on top of it:

```python
AgentFactory = Callable[[str, str], Agent]  # (user_id, user_token) -> Agent
```

`create_chatagent_v3_router(..., *, agent_factory: AgentFactory, ...)` is the
router's only coupling point to "how the brain works." Inside the POST
handler: `agent = agent_factory(user_id, raw_token)`.

---

## 3. Current implementations

| | `ADKAgent` (`/chatagent/v3`) | `DirectLLMAgent` (`/twp/v1`) |
|---|---|---|
| Backend | External ADK-style upstream service (HTTP) | ragent's own `LLMClient` |
| Caller Protocol | `twp_ai.callers.adk.ADKCaller` | `twp_ai.callers.protocol.LLMCaller` |
| Concrete caller | `ragent.clients.adk_caller.ADKCaller` | `twp_ai.callers.ragent.RagentCaller` |
| Tool loop | Owned by the upstream service | Managed by `DirectLLMAgent` itself |
| Per-request state | Yes (`user_id`/`user_token`) → needs a factory, not a singleton | No → singleton instance is enough |
| Injection site | `composition.py::_build_chatagent_agent_factory()` → `Container.chatagent_agent_factory` → `create_chatagent_v3_router(agent_factory=...)` | `bootstrap/app.py` constructs `DirectLLMAgent(RagentCaller(container.llm_client))` inline and passes it to `twp_ai.create_router()` |

---

## 4. Call chain

```
POST /chatagent/v3
  └─ chatagent_v3_post()                         [routers/chatagent_v3.py]
       agent = agent_factory(user_id, raw_token)  ← the only coupling point; typed as Agent
       agent.run(body, model)                     [twp_ai.agent.Agent Protocol]
            ── current ──→ ADKAgent.run()           [twp_ai/agents/adk.py]
                            └─ caller.stream_deltas()  [ADKCaller, ragent/clients/adk_caller.py]
                                 └─ HTTP POST → external ADK upstream
            ── future ──→ DirectLLMAgent.run() or any custom Agent
                            └─ any LLMCaller / direct library call

agent_factory assembly (composition root):
  bootstrap/composition.py::_build_chatagent_agent_factory()
       closes over http_client / api_url / ap_name / auth / timeout
       returns (user_id, user_token) -> Agent
  → Container.chatagent_agent_factory
  → bootstrap/app.py passes it to create_chatagent_v3_router(agent_factory=...)

GET /chatagent/v3/sessionList, /session (unchanged by this refactor):
  router → proxy_get/proxy_write [_chatagent_proxy.py]
         → transform=map_session_payload / map_session_list_payload
              [services/chatagent_session.py]
              └─ node_to_role()  [twp_ai.roles] ← swap together with adk_caller.py
```

---

## 5. Brain-swap runbook

1. Write a new `Agent` implementation satisfying `twp_ai.agent.Agent.run(request, model) -> Generator[str, None, None]` — reuse `DirectLLMAgent` with a custom `LLMCaller`, or write a new class from scratch.
2. In `bootstrap/composition.py`, add a new `_build_xxx_agent_factory()` and select it with a `CHATAGENT_BACKEND` env switch, assigning the result to `Container.chatagent_agent_factory`.
3. If the new backend's wire format differs from the ADK shape, write a new `map_session_payload`/`map_session_list_payload` for `services/chatagent_session.py`, switched the same way.
4. **Do not modify** `routers/chatagent_v3.py` or `routers/_chatagent_proxy.py` — that is the property this refactor exists to guarantee.

---

## 6. Zero-modification / two-touch-files checklist

Zero modification required:
- `src/ragent/routers/chatagent_v3.py`
- `src/ragent/routers/_chatagent_proxy.py`
- `src/ragent/schemas/chatagent.py`

Touch only if needed:
- `src/ragent/bootstrap/composition.py` — new factory branch + `CHATAGENT_BACKEND` switch (always touched).
- `src/ragent/services/chatagent_session.py` — only if the new backend's wire format differs from ADK's.

---

## 7. Known limitation: session history is not portable

This refactor fixes the **code-dependency direction** (the router no longer
imports a concrete `Agent`/`Caller`). It does **not** fix what happens to
**existing session history** when the upstream brain is swapped.

Today ragent has no local session-history database — `GET /chatagent/v3/session`
and `/sessionList` proxy directly to the *old* upstream's own database
(`CHATAGENT_SESSION_API_URL` / `CHATAGENT_SESSIONLIST_API_URL`). Swapping to a
new backend means old sessions are not in a different *format* — they are not
present at all in the new backend's storage.

When a real brain-swap happens, pick one (not decided in advance):

1. **Dual-track coexistence** — old session ids keep proxying to the old
   upstream for reads; new sessions go to the new backend. No cross-migration.
2. **ragent-owned session-history DB** — ragent takes over persistence itself
   (architecture-level change), independent of whichever backend is active.
3. **One-time ETL** — migrate the old upstream's session data into the new
   backend's storage on cutover day (requires the new backend to expose a
   write API for historical data).

This is explicitly out of scope here — see `src/ragent/services/chatagent_session.py`'s
module docstring, which documents the same coupling at the code level.
