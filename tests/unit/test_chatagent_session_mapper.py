"""Session-history mapping for /chatagent/v3 — twp-ai roles + hidden stripped.

The upstream persists every turn verbatim (memory by session), so the stored
history carries both raw upstream roles (`assistant`/`tool`) keyed by
`messageMeta.langgraph_node` and the `<hidden>` context/state preamble on the
user turn. The v3 session endpoint maps each message to a twp-ai role and strips
the hidden block before returning it to the client.
"""

import json

from ragent.services.chatagent_session import map_session_list_payload, map_session_payload


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

    assert out["messages"] == [
        {
            "id": "m1",
            "role": "user",
            "content": "What is X?",
            "createTime": None,
            "updateTime": None,
            "attachments": None,
        }
    ]


def test_legacy_bare_context_block_is_stripped() -> None:
    # Sessions created before v3 carry a bare <context> block (no <hidden>).
    payload = _session(
        [
            {
                "messageId": "m1",
                "role": "user",
                "content": "<context>\ncurrent page\n</context>\n\nWhat is X?",
            }
        ]
    )

    out = map_session_payload(payload)

    assert out["messages"] == [
        {
            "id": "m1",
            "role": "user",
            "content": "What is X?",
            "createTime": None,
            "updateTime": None,
            "attachments": None,
        }
    ]


def test_interrupt_turn_is_filtered_from_history() -> None:
    # An upstream HITL interrupt turn is a transient approval prompt, not a
    # conversation message — consistent with the v3 stream (where it goes to
    # RUN_FINISHED.outcome, never the message flow), it must not render in history.
    payload = _session(
        [
            {"messageId": "u1", "role": "user", "content": "delete everything"},
            {
                "messageId": "hitl-1",
                "role": "assistant",
                "content": "Confirm?",
                "humanInTheLoopMeta": {"isInterrupt": True, "interruptMessage": "Confirm?"},
            },
            {"messageId": "a1", "role": "assistant", "content": "Done."},
        ]
    )

    out = map_session_payload(payload)

    ids = [m["id"] for m in out["messages"]]
    assert ids == ["u1", "a1"]  # the interrupt turn is dropped


def test_non_interrupt_hitl_meta_is_not_filtered() -> None:
    # humanInTheLoopMeta present but isInterrupt false/absent stays in history.
    payload = _session(
        [
            {
                "messageId": "a1",
                "role": "assistant",
                "content": "hi",
                "humanInTheLoopMeta": {"isInterrupt": False},
            }
        ]
    )

    out = map_session_payload(payload)

    assert [m["id"] for m in out["messages"]] == ["a1"]


def test_double_encoded_content_is_unwrapped_then_stripped() -> None:
    # Upstream stores content JSON-double-encoded: a quoted string with literal
    # \n escapes. Decode that layer first, else `"\n\nWhat is X?"` survives.
    real = "<hidden>\n<context>[]</context>\n</hidden>\n\nWhat is X?"
    payload = _session([{"messageId": "m1", "role": "user", "content": json.dumps(real)}])

    out = map_session_payload(payload)

    assert out["messages"][0]["content"] == "What is X?"


def test_double_encoded_session_name_is_unwrapped_then_stripped() -> None:
    real = "<context>page</context>\n\nFirst chat"
    payload = {"session": "s1", "sessionName": json.dumps(real), "messages": []}

    assert map_session_payload(payload)["sessionName"] == "First chat"


def test_plain_content_is_not_json_unwrapped() -> None:
    # A plain message that isn't a JSON string must pass through untouched.
    payload = _session([{"messageId": "m1", "role": "user", "content": "What is X?"}])

    assert map_session_payload(payload)["messages"][0]["content"] == "What is X?"


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


def test_non_dict_payload_is_returned_unchanged() -> None:
    # A malformed upstream (array/scalar) must not raise AttributeError.
    assert map_session_payload([1, 2]) == [1, 2]  # type: ignore[arg-type]


def test_session_name_is_stripped_on_get() -> None:
    # sessionName is derived from the first user turn, which carries the wrapper.
    payload = {
        "session": "s1",
        "sessionName": "<hidden>\n<context>[]</context>\n</hidden>\n\nWhat is X?",
        "messages": [],
    }

    out = map_session_payload(payload)

    assert out["sessionName"] == "What is X?"


def test_session_list_strips_each_session_name() -> None:
    payload = {
        "totalCount": 2,
        "sessions": [
            {"session": "s1", "sessionName": "<context>page</context>\n\nFirst"},
            {"session": "s2", "sessionName": "Plain title"},
        ],
    }

    out = map_session_list_payload(payload)

    assert [s["sessionName"] for s in out["sessions"]] == ["First", "Plain title"]
    assert out["totalCount"] == 2


def test_session_list_without_sessions_is_returned_unchanged() -> None:
    assert map_session_list_payload({"totalCount": 0}) == {"totalCount": 0}


def test_session_list_merges_status_when_status_of_provided() -> None:
    # T-CAv3L: each entry gains {running, hasNewReply} from the injected status fn,
    # keyed by its session id, on top of the existing sessionName strip.
    payload = {
        "sessions": [
            {"session": "t1", "sessionName": "A"},
            {"session": "t2", "sessionName": "B"},
        ]
    }
    status = {
        "t1": {"running": True, "hasNewReply": False},
        "t2": {"running": False, "hasNewReply": True},
    }

    out = map_session_list_payload(payload, status.__getitem__)

    assert out["sessions"][0] == {
        "session": "t1",
        "sessionName": "A",
        "running": True,
        "hasNewReply": False,
    }
    assert out["sessions"][1] == {
        "session": "t2",
        "sessionName": "B",
        "running": False,
        "hasNewReply": True,
    }


def test_session_list_status_of_none_leaves_entries_unchanged() -> None:
    # No store wired → status_of is None → the list degrades to title-only (the
    # pre-T-CAv3L behaviour), never raising.
    payload = {"sessions": [{"session": "t1", "sessionName": "A"}]}

    out = map_session_list_payload(payload, None)

    assert out["sessions"][0] == {"session": "t1", "sessionName": "A"}


def test_session_list_entry_without_session_id_skips_status() -> None:
    # A malformed entry with no usable session id must not crash status lookup.
    payload = {"sessions": [{"sessionName": "no id"}]}

    out = map_session_list_payload(payload, lambda _sid: {"running": True})

    assert out["sessions"][0] == {"sessionName": "no id"}


def test_explicit_null_role_falls_back_to_assistant() -> None:
    payload = _session([{"messageId": "m1", "role": None, "content": "x"}])

    assert map_session_payload(payload)["messages"][0]["role"] == "assistant"


def test_message_carries_upstream_create_and_update_time() -> None:
    # The upstream persists each turn with createTime/updateTime; the reshaped
    # message must surface them so the client can render per-message timestamps.
    payload = _session(
        [
            {
                "messageId": "m1",
                "role": "user",
                "content": "What is X?",
                "createTime": "2025-05-01T06:48:55.617Z",
                "updateTime": "2025-05-01T06:49:00.000Z",
            }
        ]
    )

    out = map_session_payload(payload)

    assert out["messages"][0]["createTime"] == "2025-05-01T06:48:55.617Z"
    assert out["messages"][0]["updateTime"] == "2025-05-01T06:49:00.000Z"


def test_message_without_timestamps_yields_null_fields() -> None:
    # A malformed upstream message missing the timestamps must not raise; the
    # fields pass through as null rather than vanishing from the shape.
    payload = _session([{"messageId": "m1", "role": "user", "content": "x"}])

    out = map_session_payload(payload)

    assert out["messages"][0]["createTime"] is None
    assert out["messages"][0]["updateTime"] is None


def test_message_with_attachments_block_surfaces_attachments_field() -> None:
    # docs/spec/chat_attachments.md §8: session-history reads must parse
    # <attachments> from the hidden preamble before it is stripped, so the
    # client can render which attachment(s) a historical turn carried.
    payload = _session(
        [
            {
                "messageId": "m1",
                "role": "user",
                "content": (
                    '<hidden>\n<attachments>[{"attachmentId":"att_1",'
                    '"filename":"report.pdf","mimeType":"application/pdf",'
                    '"sizeBytes":1024}]</attachments>\n<context>[]</context>\n</hidden>'
                    "\n\nSummarize this"
                ),
            }
        ]
    )

    out = map_session_payload(payload)

    assert out["messages"][0]["content"] == "Summarize this"
    assert out["messages"][0]["attachments"] == [
        {
            "attachmentId": "att_1",
            "filename": "report.pdf",
            "mimeType": "application/pdf",
            "sizeBytes": 1024,
        }
    ]


def test_message_without_attachments_block_yields_null_attachments_field() -> None:
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

    assert out["messages"][0]["attachments"] is None


def test_double_encoded_message_with_attachments_is_unwrapped_then_extracted() -> None:
    # Same double-encoding artifact as content/sessionName (module docstring):
    # the <attachments> tag must still be found after the JSON-string layer
    # is decoded, not only after strip_machine_context runs.
    real = (
        '<hidden>\n<attachments>[{"attachmentId":"att_1","filename":"a.txt",'
        '"mimeType":"text/plain","sizeBytes":4}]</attachments>\n<context>[]</context>'
        "\n</hidden>\n\nWhat is X?"
    )
    payload = _session([{"messageId": "m1", "role": "user", "content": json.dumps(real)}])

    out = map_session_payload(payload)

    assert out["messages"][0]["content"] == "What is X?"
    assert out["messages"][0]["attachments"][0]["attachmentId"] == "att_1"


def test_non_string_content_yields_null_attachments_field() -> None:
    # Mirrors the existing non-str content passthrough (content stays as-is);
    # attachments extraction must not raise on a non-str content value.
    payload = _session([{"messageId": "m1", "role": "user", "content": {"weird": "shape"}}])

    out = map_session_payload(payload)

    assert out["messages"][0]["attachments"] is None
