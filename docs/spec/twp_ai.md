# twp-ai Adapter (`packages/twp-ai`)

> Mounted at `POST /twp/v1/run`. Standard auth applies (`X-User-Id` or `X-Auth-Token`).  
> Full API examples: [`docs/API.md §twp-ai`](../API.md#twp-ai).

---

## Run input

Required fields: `threadId`, `runId`, `state`, `messages`, `tools`, `context`, `forwardedProps`. Optional: `parentRunId`, `model` (falls back to `TWP_DEFAULT_MODEL` env var).

```json
{
  "threadId": "thread_1",
  "runId": "run_1",
  "parentRunId": null,
  "state": { "page": { "title": "Edit product" } },
  "messages": [{ "id": "msg_user_1", "role": "user", "content": "Fill the description" }],
  "tools": [
    {
      "name": "fill_form",
      "description": "Use only when the user asks to fill or update form fields.",
      "parameters": { "type": "object", "properties": { "description": { "type": "string" } } }
    }
  ],
  "context": [{ "description": "Current page", "value": "{\"title\":\"Edit product\",\"fields\":[\"description\"]}" }],
  "forwardedProps": { "source": "form-page" },
  "model": "gptoss-120b"
}
```

`context` carries twp-ai contextual items. Tool availability belongs in the top-level `tools` array.

---

## Stream events

`text/event-stream`. Each event is a `data: {…}\n\n` line carrying a camelCase JSON payload.

| Event type | When emitted |
|---|---|
| `RUN_STARTED` | Beginning of every run |
| `TEXT_MESSAGE_START` | LLM starts emitting text |
| `TEXT_MESSAGE_CONTENT` | Each streaming delta |
| `TEXT_MESSAGE_END` | LLM text block complete |
| `TOOL_CALL_START` | LLM invokes a client-side tool |
| `TOOL_CALL_ARGS` | Tool argument fragment |
| `TOOL_CALL_END` | Tool-call block complete |
| `RUN_FINISHED` | Run complete (no error) |
| `RUN_ERROR` | Run failed; `message`, `code` fields present |

`tool_choice=auto` — the LLM decides whether to call a tool. `TOOL_CALL_ARGS` may be emitted as a single complete delta when the underlying provider adapter only exposes accumulated arguments.

---

## Tool result boundary

The direct runtime does **not** synthesize a tool result or run a confirmation turn. After emitting the tool-call lifecycle events it finishes the run, yielding control to the frontend. The frontend executes the tool and sends the result back as a `role="tool"` message in a continuation run; the runtime translates prior tool-call and tool-result messages into provider-compatible messages so the next LLM turn sees the actual outcome.
