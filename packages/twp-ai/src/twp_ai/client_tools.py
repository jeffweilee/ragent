"""AGENTIC_UI_TOOL — the client-side tool dispatcher for the ADK upstream.

The ADK upstream only invokes tools pre-registered in its own registry; it
cannot accept the per-request `tools` a twp-ai client provides. To bridge that,
a single generic tool — `AGENTIC_UI_TOOL` — is pre-registered upstream as a
client-side (emit-and-suspend) dispatcher. The upstream calls it with the chosen
frontend tool wrapped inside its arguments, shaped:

    {"tool_name": "<frontend tool>", "arguments": {<that tool's args>}}

`ADKAgent._relay` unwraps that envelope so the frontend receives a normal
tool-call for the real tool (it never sees `AGENTIC_UI_TOOL`). The per-request
catalog of available frontend tools is surfaced to the upstream separately (the
`<tools>` section of the ragent caller's `<hidden>` machine-context block).
"""

from __future__ import annotations

import json
from typing import Any

AGENTIC_UI_TOOL_NAME = "AGENTIC_UI_TOOL"


def unwrap_agentic_ui_call(arguments: str | dict[str, Any]) -> tuple[str, str]:
    """Unwrap an `AGENTIC_UI_TOOL` call's arguments.

    `arguments` is the JSON string the upstream emitted for the dispatcher's
    function call (a pre-parsed `dict` is also tolerated — some providers emit
    structured arguments). Returns `(inner_tool_name, inner_arguments_json)` —
    the real frontend tool name and its arguments re-serialised as a JSON string
    (the wire shape `TOOL_CALL_ARGS.delta` expects). Raises `ValueError` (which
    `json.JSONDecodeError` subclasses) when the envelope is missing or malformed,
    so the relay can surface a single `RUN_ERROR`.
    """
    if isinstance(arguments, dict):
        envelope: Any = arguments
    elif isinstance(arguments, str):
        envelope = json.loads(arguments)
    else:
        raise ValueError("AGENTIC_UI_TOOL arguments must be a JSON string or object")
    if not isinstance(envelope, dict):
        raise ValueError("AGENTIC_UI_TOOL arguments must be a JSON object")
    tool_name = envelope.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name:
        raise ValueError("AGENTIC_UI_TOOL arguments missing a non-empty 'tool_name'")
    # A null/absent inner `arguments` degrades to {}; a non-object (string/array)
    # is malformed — serialising it would emit non-object args the frontend tool
    # cannot consume.
    inner = envelope.get("arguments")
    if inner is None:
        inner = {}
    elif not isinstance(inner, dict):
        raise ValueError("AGENTIC_UI_TOOL 'arguments' must be a JSON object")
    return tool_name, json.dumps(inner, ensure_ascii=False)
