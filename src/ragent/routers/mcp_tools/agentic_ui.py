"""MCP tool descriptor for `AGENTIC_UI_TOOL` (T-CAUI.4).

The client-side tool dispatcher pre-registered for the ADK upstream. The upstream
discovers it via `tools/list`, then calls it with the chosen frontend tool wrapped
inside (`{tool_name, arguments}`); ragent's ADK relay unwraps that so the frontend
sees a normal tool call for the real tool. It is **client-side**: the frontend
executes it, so a server-side `tools/call` for it is rejected (see `mcp.py`).
"""

from __future__ import annotations

from mcp.types import Tool
from twp_ai.client_tools import AGENTIC_UI_TOOL_NAME

AGENTIC_UI_TOOL = Tool(
    name=AGENTIC_UI_TOOL_NAME,
    description=(
        "Invoke a frontend (client-side) UI tool on the user's behalf. The set of "
        "frontend tools available this turn — each with its JSON Schema — is listed "
        "in the <tools> section of the machine-context <hidden> block. Choose the "
        "matching tool and pass its name as `tool_name` and its arguments object as "
        "`arguments`. The frontend executes the tool; do not fabricate a result."
    ),
    inputSchema={
        "type": "object",
        "additionalProperties": False,
        "required": ["tool_name", "arguments"],
        "properties": {
            "tool_name": {
                "type": "string",
                "description": "Name of the frontend tool to invoke (from the <tools> catalog).",
            },
            "arguments": {
                "type": "object",
                "description": "Arguments object conforming to that tool's JSON Schema.",
            },
        },
    },
)
