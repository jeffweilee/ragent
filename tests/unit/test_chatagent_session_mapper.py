"""Session-history mapping for /chatagent/v3 — twp-ai roles + hidden stripped.

The upstream persists every turn verbatim (memory by session), so the stored
history carries both raw upstream roles (`assistant`/`tool`) keyed by
`messageMeta.langgraph_node` and the `<hidden>` context/state preamble on the
user turn. The v3 session endpoint maps each message to a twp-ai role and strips
the hidden block before returning it to the client.
"""

from ragent.services.chatagent_session import map_session_payload


def _session(messages: list[dict]) -> dict:
    return {"session": "s1", "sessionName": "chat", "messages": messages}


def test_user_message_keeps_user_role_and_strips_hidden() -> None:
    payload = _session(
        [
            {
                "messageId": "m1",
                "role": "user",
                "content": "<hidden>\n<context>[]</context>\n</hidden>\n\nWhat is X?",
            }
        ]
    )

    out = map_session_payload(payload)

    assert out["messages"] == [{"id": "m1", "role": "user", "content": "What is X?"}]


def test_planner_node_maps_to_reasoning() -> None:
    payload = _session(
        [
            {
                "messageId": "m2",
                "role": "assistant",
                "content": "Planning...",
                "messageMeta": {"langgraph_node": "planner"},
            }
        ]
    )

    out = map_session_payload(payload)

    assert out["messages"][0]["role"] == "reasoning"
    assert out["messages"][0]["content"] == "Planning..."


def test_other_assistant_nodes_map_to_assistant() -> None:
    payload = _session(
        [
            {
                "messageId": "m3",
                "role": "assistant",
                "content": "Done.",
                "messageMeta": {"langgraph_node": "summarizer"},
            }
        ]
    )

    assert map_session_payload(payload)["messages"][0]["role"] == "assistant"


def test_tool_role_maps_to_tool() -> None:
    payload = _session([{"messageId": "m4", "role": "tool", "content": "result"}])

    assert map_session_payload(payload)["messages"][0]["role"] == "tool"


def test_envelope_fields_are_preserved() -> None:
    payload = _session([])
    payload["sessionStatus"] = "active"

    out = map_session_payload(payload)

    assert out["session"] == "s1"
    assert out["sessionName"] == "chat"
    assert out["sessionStatus"] == "active"
    assert out["messages"] == []


def test_payload_without_messages_list_is_returned_unchanged() -> None:
    payload = {"session": "s1", "sessionName": "chat"}

    assert map_session_payload(payload) == payload
