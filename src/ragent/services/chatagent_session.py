"""Map upstream ChatAgent session history into the twp-ai message shape.

The upstream keeps conversation memory by `session` and persists every turn
verbatim. Its stored history therefore carries raw upstream roles
(`assistant`/`tool`) discriminated by `messageMeta.langgraph_node`, plus the
`<hidden>` context/state preamble the v3 caller prepends to the user turn. The
v3 session endpoint surfaces a clean history: each message is relabelled to a
twp-ai role (shared `node_to_role` rule — identical to the v3 stream) and the
machine-context wrapper (`<hidden>`, or a legacy bare `<context>` for sessions
created before v3) is stripped from its content. Human-in-the-loop interrupt
turns (`humanInTheLoopMeta.isInterrupt`) are dropped entirely: like the v3
stream (which surfaces them via `RUN_FINISHED.outcome`, not the message flow),
they are transient approval prompts, not conversation messages.

The transform preserves the upstream session envelope (`session`, …) and
rewrites `messages[]`. `sessionName` is also stripped because the upstream
derives it from the first user turn, which carries the wrapper — so the same
machine-context block would otherwise leak into the session title (here and in
the session list).

Some upstream `content`/`sessionName` values are JSON-double-encoded (a quoted
string with literal `\\n` escapes). That layer is decoded first, otherwise the
wrapper strip sees a leading `"` and literal `\\n` (not whitespace) and leaves
`"\\n\\n<message>"` behind.

This module and `ragent/clients/adk_caller.py` are the same backend-adapter
pair: swapping the upstream agent to a different wire format means replacing
both together, not just one.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from twp_ai.roles import node_to_role

from ragent.utility.hidden import strip_machine_context

_ATTACHMENTS_PATTERN = re.compile(r"<attachments>(.*?)</attachments>", re.DOTALL)


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


def _extract_attachments_from_hidden(hidden_block: str) -> list[dict[str, Any]] | None:
    """Extract attachments from <hidden> preamble before stripping.

    Searches for <attachments>…</attachments> block within the hidden preamble
    and parses it as JSON. Returns the parsed list, or None if the block is
    absent or empty.
    """
    match = _ATTACHMENTS_PATTERN.search(hidden_block)
    if not match:
        return None

    content = match.group(1).strip()
    if not content:
        return None

    try:
        parsed = json.loads(content)
        if isinstance(parsed, list) and len(parsed) > 0:
            return parsed
    except (ValueError, TypeError):
        pass

    return None


def map_session_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return payload
    out = _strip_session_name(payload)
    messages = out.get("messages")
    if isinstance(messages, list):
        # Drop human-in-the-loop interrupt turns: they are transient approval
        # prompts (surfaced live via RUN_FINISHED.outcome), not conversation
        # messages, so they must not render in the persisted history — same
        # control-plane treatment as the v3 stream.
        out = {
            **out,
            "messages": [
                _map_message(m) for m in messages if isinstance(m, dict) and not _is_interrupt(m)
            ],
        }
    return out


def _is_interrupt(raw: dict[str, Any]) -> bool:
    hitl = raw.get("humanInTheLoopMeta")
    return isinstance(hitl, dict) and bool(hitl.get("isInterrupt"))


def map_session_list_payload(
    payload: dict[str, Any],
    status_of: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return payload
    sessions = payload.get("sessions")
    if not isinstance(sessions, list):
        return payload
    mapped = [_map_session_entry(s, status_of) if isinstance(s, dict) else s for s in sessions]
    return {**payload, "sessions": mapped}


def _map_session_entry(
    entry: dict[str, Any], status_of: Callable[[str], dict[str, Any]] | None
) -> dict[str, Any]:
    entry = _strip_session_name(entry)
    if status_of is None:
        return entry
    session_id = entry.get("session")
    if not isinstance(session_id, str):
        return entry
    # Live status (running spinner + new-reply dot) is merged onto the stripped
    # entry; the status fn closes over the store + caller user_id in the router.
    return {**entry, **status_of(session_id)}


def _strip_session_name(entry: dict[str, Any]) -> dict[str, Any]:
    name = entry.get("sessionName")
    if not isinstance(name, str):
        return entry
    return {**entry, "sessionName": _clean_text(name)}


def _map_message(raw: dict[str, Any]) -> dict[str, Any]:
    meta = raw.get("messageMeta")
    langgraph_node = meta.get("langgraph_node") if isinstance(meta, dict) else None
    content = raw.get("content")
    # Unwrap once and reuse for both attachment extraction and the content
    # strip below — extraction must run on the un-stripped block (it reads
    # the <attachments> tag strip_machine_context removes).
    unwrapped = _unwrap_json_string(content) if isinstance(content, str) else None
    # `or "assistant"`: a present-but-null `role` must fall back too, not just a
    # missing key — keeps a non-empty string for node_to_role.
    return {
        "id": raw.get("messageId") or "",
        "role": node_to_role(raw.get("role") or "assistant", langgraph_node),
        "content": strip_machine_context(unwrapped) if unwrapped is not None else content,
        # Pass the upstream persistence timestamps through so the client can
        # render per-message create/update times (null when upstream omits them).
        "createTime": raw.get("createTime"),
        "updateTime": raw.get("updateTime"),
        "attachments": _extract_attachments_from_hidden(unwrapped)
        if unwrapped is not None
        else None,
    }
