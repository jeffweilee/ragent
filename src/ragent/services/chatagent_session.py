"""Map upstream ChatAgent session history into the twp-ai message shape.

The upstream keeps conversation memory by `session` and persists every turn
verbatim. Its stored history therefore carries raw upstream roles
(`assistant`/`tool`) discriminated by `messageMeta.langgraph_node`, plus the
`<hidden>` context/state preamble the v3 caller prepends to the user turn. The
v3 session endpoint surfaces a clean history: each message is relabelled to a
twp-ai role (shared `node_to_role` rule — identical to the v3 stream) and the
hidden block is stripped from its content.

The transform preserves the upstream session envelope (`session`,
`sessionName`, …) and only rewrites `messages[]`; payloads without a messages
list pass through untouched.
"""

from __future__ import annotations

from typing import Any

from twp_ai.roles import node_to_role

from ragent.utility.hidden import strip_hidden


def map_session_payload(payload: dict[str, Any]) -> dict[str, Any]:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return payload
    mapped = [_map_message(m) for m in messages if isinstance(m, dict)]
    return {**payload, "messages": mapped}


def _map_message(raw: dict[str, Any]) -> dict[str, Any]:
    meta = raw.get("messageMeta")
    langgraph_node = meta.get("langgraph_node") if isinstance(meta, dict) else None
    content = raw.get("content")
    return {
        "id": raw.get("messageId") or "",
        "role": node_to_role(raw.get("role", "assistant"), langgraph_node),
        "content": strip_hidden(content) if isinstance(content, str) else content,
    }
