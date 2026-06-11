"""Shared upstream-role classification for the ChatAgent (ADK) protocol.

A single source of truth for mapping an upstream ChatAgent message
(`role` + `messageMeta.langgraph_node`) to a twp-ai role. Used by both the
streaming `ADKAgent` (to decide a reasoning vs a text block) and the session
history mapper (to label persisted messages). Keeping the rule here prevents
the two surfaces from drifting — they must classify a message identically.
"""

from __future__ import annotations

from typing import Literal

# messageMeta.langgraph_node whose output is the agent's plan/reasoning step,
# surfaced as a reasoning block / role instead of visible assistant text.
REASONING_NODE = "planner"

TwpAiRole = Literal["user", "assistant", "reasoning", "tool"]


def node_to_role(upstream_role: str, langgraph_node: str | None) -> TwpAiRole:
    """Classify an upstream message into a twp-ai role.

    `user` input and `tool` results pass through by their upstream role; an
    `assistant` message is `reasoning` when it comes from the planner node and
    `assistant` otherwise.
    """
    if upstream_role == "user":
        return "user"
    if upstream_role == "tool":
        return "tool"
    if langgraph_node == REASONING_NODE:
        return "reasoning"
    return "assistant"
