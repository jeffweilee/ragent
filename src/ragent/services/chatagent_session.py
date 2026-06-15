"""Map upstream ChatAgent session history into the twp-ai message shape.

The upstream keeps conversation memory by `session` and persists every turn
verbatim. Its stored history therefore carries raw upstream roles
(`assistant`/`tool`) discriminated by `messageMeta.langgraph_node`, plus the
`<hidden>` context/state preamble the v3 caller prepends to the user turn. The
v3 session endpoint surfaces a clean history: each message is relabelled to a
twp-ai role (shared `node_to_role` rule — identical to the v3 stream) and the
machine-context wrapper (`<hidden>`, or a legacy bare `<context>` for sessions
created before v3) is stripped from its content.

The transform preserves the upstream session envelope (`session`, …) and
rewrites `messages[]`. `sessionName` is also stripped because the upstream
derives it from the first user turn, which carries the wrapper — so the same
machine-context block would otherwise leak into the session title (here and in
the session list).

Some upstream `content`/`sessionName` values are JSON-double-encoded (a quoted
string with literal `\\n` escapes). That layer is decoded first, otherwise the
wrapper strip sees a leading `"` and literal `\\n` (not whitespace) and leaves
`"\\n\\n<message>"` behind.
"""

from __future__ import annotations

import json
from typing import Any

from twp_ai.roles import node_to_role

from ragent.utility.hidden import strip_machine_context


def _clean_text(value: str) -> str:
    return strip_machine_context(_unwrap_json_string(value))


def _unwrap_json_string(value: str) -> str:
    # Only collapse a JSON-encoded *string* (the double-encoding artifact); leave
    # plain text — and JSON numbers/objects/arrays — untouched.
    try:
        decoded = json.loads(value)
    except (ValueError, TypeError):
        return value
    return decoded if isinstance(decoded, str) else value


def map_session_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return payload
    out = _strip_session_name(payload)
    messages = out.get("messages")
    if isinstance(messages, list):
        out = {**out, "messages": [_map_message(m) for m in messages if isinstance(m, dict)]}
    return out


def map_session_list_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return payload
    sessions = payload.get("sessions")
    if not isinstance(sessions, list):
        return payload
    stripped = [_strip_session_name(s) if isinstance(s, dict) else s for s in sessions]
    return {**payload, "sessions": stripped}


def _strip_session_name(entry: dict[str, Any]) -> dict[str, Any]:
    name = entry.get("sessionName")
    if not isinstance(name, str):
        return entry
    return {**entry, "sessionName": _clean_text(name)}


def _map_message(raw: dict[str, Any]) -> dict[str, Any]:
    meta = raw.get("messageMeta")
    langgraph_node = meta.get("langgraph_node") if isinstance(meta, dict) else None
    content = raw.get("content")
    # `or "assistant"`: a present-but-null `role` must fall back too, not just a
    # missing key — keeps a non-empty string for node_to_role.
    return {
        "id": raw.get("messageId") or "",
        "role": node_to_role(raw.get("role") or "assistant", langgraph_node),
        "content": _clean_text(content) if isinstance(content, str) else content,
        # Pass the upstream persistence timestamps through so the client can
        # render per-message create/update times (null when upstream omits them).
        "createTime": raw.get("createTime"),
        "updateTime": raw.get("updateTime"),
    }
