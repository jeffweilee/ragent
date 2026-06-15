"""T-CVQ.1 — Pure checker functions for admin quality validation."""

from __future__ import annotations

import json

from ragent.utility.hidden import strip_machine_context

_TOOL_EVENT_TYPES = frozenset(
    {"TOOL_CALL_START", "TOOL_CALL_ARGS", "TOOL_CALL_END", "TOOL_CALL_RESULT"}
)


def parse_sse_line(line: str) -> dict | None:
    """Parse one SSE data line. Returns None for keep-alive or non-data lines."""
    if not line.startswith("data: "):
        return None
    payload = line[6:]
    if payload == "[Done]":
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None


def collect_text(events: list[dict]) -> str:
    """Concatenate all TEXT_MESSAGE_CONTENT deltas into a single string."""
    return "".join(e.get("delta", "") for e in events if e.get("type") == "TEXT_MESSAGE_CONTENT")


def check_keywords_any(text: str, keywords: list[str]) -> list[str]:
    """Return failure reasons if none of the keywords appear (case-insensitive)."""
    if not keywords:
        return []
    lower = text.lower()
    if any(kw.lower() in lower for kw in keywords):
        return []
    quoted = ", ".join(repr(kw) for kw in keywords)
    return [f"none of the expected keywords found: {quoted}"]


def check_no_keywords(text: str, keywords: list[str]) -> list[str]:
    """Return failure reasons for each forbidden keyword found (case-insensitive)."""
    lower = text.lower()
    return [f"forbidden keyword found: {kw!r}" for kw in keywords if kw.lower() in lower]


def check_protocol(events: list[dict], *, expect_no_tool_calls: bool = False) -> list[str]:
    """
    Validate SSE event sequence.  Returns a list of violation strings
    (empty = compliant).
    """
    violations: list[str] = []
    types = [e.get("type") for e in events]

    if not types or types[0] != "RUN_STARTED":
        violations.append("missing RUN_STARTED at stream start")

    has_terminal = "RUN_FINISHED" in types or "RUN_ERROR" in types
    if not has_terminal:
        violations.append("missing RUN_FINISHED or RUN_ERROR")

    if "RUN_ERROR" in types:
        err = next(e for e in events if e.get("type") == "RUN_ERROR")
        violations.append(
            f"RUN_ERROR received: code={err.get('code', '')!r} message={err.get('message', '')!r}"
        )

    violations.extend(_check_text_blocks(events))
    violations.extend(_check_reasoning_blocks(events))
    violations.extend(_check_tool_call_blocks(events))

    if expect_no_tool_calls:
        found = next((e.get("type") for e in events if e.get("type") in _TOOL_EVENT_TYPES), None)
        if found:
            violations.append(f"unexpected TOOL_CALL event: {found!r}")

    return violations


def check_session_messages(messages: list[dict], *, keywords_any: list[str]) -> list[str]:
    """
    Validate reshaped session messages from GET /chatagent/v3/session.
    Returns violation strings.
    """
    violations: list[str] = []

    if len(messages) < 2:
        violations.append(f"expected ≥2 session messages, got {len(messages)}")
        return violations

    if messages[0].get("role") != "user":
        violations.append(
            f"first session message role is {messages[0].get('role')!r}, expected 'user'"
        )

    assistant_msgs = [m for m in messages if m.get("role") == "assistant"]
    if not assistant_msgs:
        violations.append("no assistant message in session")
        return violations

    for i, msg in enumerate(messages):
        content = msg.get("content") or ""
        if strip_machine_context(content) != content:
            violations.append(f"machine-context tag leaked in session message {i + 1}")

    if keywords_any:
        session_text = " ".join(m.get("content") or "" for m in assistant_msgs)
        for reason in check_keywords_any(session_text, keywords_any):
            violations.append(f"[session] {reason}")

    return violations


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _check_text_blocks(events: list[dict]) -> list[str]:
    return _check_message_blocks(
        events,
        start_type="TEXT_MESSAGE_START",
        content_type="TEXT_MESSAGE_CONTENT",
        end_type="TEXT_MESSAGE_END",
    )


def _check_reasoning_blocks(events: list[dict]) -> list[str]:
    return _check_message_blocks(
        events,
        start_type="REASONING_MESSAGE_START",
        content_type="REASONING_MESSAGE_CONTENT",
        end_type="REASONING_MESSAGE_END",
    )


def _check_message_blocks(
    events: list[dict],
    *,
    start_type: str,
    content_type: str,
    end_type: str,
) -> list[str]:
    violations: list[str] = []
    open_blocks: dict[str, bool] = {}  # message_id → has_content

    for e in events:
        t = e.get("type")
        mid = e.get("messageId")
        if t == start_type and mid:
            open_blocks[mid] = False
        elif t == content_type and mid in open_blocks:
            open_blocks[mid] = True
        elif t == end_type and mid in open_blocks:
            if not open_blocks[mid]:
                violations.append(f"{start_type} {mid!r}: block closed with no content")
            del open_blocks[mid]

    for mid in open_blocks:
        violations.append(f"{start_type} {mid!r}: no matching {end_type}")

    return violations


def _check_tool_call_blocks(events: list[dict]) -> list[str]:
    violations: list[str] = []
    open_calls: set[str] = set()
    closed_calls: set[str] = set()

    for e in events:
        t = e.get("type")
        tc_id = e.get("toolCallId")
        if t == "TOOL_CALL_START" and tc_id:
            open_calls.add(tc_id)
        elif t == "TOOL_CALL_END" and tc_id:
            open_calls.discard(tc_id)
            closed_calls.add(tc_id)
        elif t == "TOOL_CALL_RESULT" and tc_id and tc_id not in closed_calls:
            violations.append(f"TOOL_CALL_RESULT before TOOL_CALL_END: {tc_id!r}")

    for tc_id in open_calls:
        violations.append(f"TOOL_CALL_START {tc_id!r}: no matching TOOL_CALL_END")

    return violations
