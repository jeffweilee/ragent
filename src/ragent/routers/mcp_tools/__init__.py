"""MCP tool descriptors for `POST /mcp/v1` (§3.8).

Each sub-module defines one tool: its input model, inputSchema, and mcp.types.Tool
descriptor. To add a new tool:
- tools/list: create a sub-module, import its Tool descriptor in mcp.py, append to
  _ALL_TOOLS — no logic change needed in _handle_tools_list.
- tools/call: also add a handler function/branch in _handle_tools_call for the new
  tool's validation and execution logic.
"""
