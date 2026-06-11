"""Shared upstream-role classification for the ChatAgent (ADK) protocol."""

from twp_ai.roles import REASONING_NODE, node_to_role


def test_user_role_passes_through() -> None:
    assert node_to_role("user", None) == "user"


def test_tool_role_maps_to_tool() -> None:
    assert node_to_role("tool", None) == "tool"
    # langgraph_node is ignored for tool results.
    assert node_to_role("tool", "summarizer") == "tool"


def test_assistant_planner_node_maps_to_reasoning() -> None:
    assert node_to_role("assistant", REASONING_NODE) == "reasoning"
    assert node_to_role("assistant", "planner") == "reasoning"


def test_assistant_other_nodes_map_to_assistant() -> None:
    assert node_to_role("assistant", "commander") == "assistant"
    assert node_to_role("assistant", "summarizer") == "assistant"
    assert node_to_role("assistant", None) == "assistant"
