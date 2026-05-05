"""T3.17 — build_rag_messages: context injection into user message + system prompt routing."""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import patch

from ragent.schemas.chat import ChatRequest, build_rag_messages, normalize_messages


def _req(*messages: dict) -> ChatRequest:
    return ChatRequest(messages=list(messages))


def _doc(content: str = "excerpt text", **meta) -> SimpleNamespace:
    return SimpleNamespace(content=content, meta=meta)


# --- no docs ---


def test_no_docs_no_user_system_matches_normalize_messages():
    req = _req({"role": "user", "content": "hello"})
    assert build_rag_messages(req, None) == normalize_messages(req)
    assert build_rag_messages(req, []) == normalize_messages(req)


def test_no_docs_with_user_system_unchanged():
    req = _req(
        {"role": "system", "content": "Custom persona"},
        {"role": "user", "content": "hello"},
    )
    assert build_rag_messages(req, []) == list(req.messages)


# --- docs present: system prompt routing ---


def test_docs_present_prepends_rag_system_at_index_0_and_wraps_last_user():
    doc = _doc("some excerpt", source_title="Wiki", document_id="d1", source_app="confluence")
    req = _req({"role": "user", "content": "What is X?"})
    result = build_rag_messages(req, [doc])

    assert result[0]["role"] == "system"
    last_user = result[-1]
    assert last_user["role"] == "user"
    assert "=== CONTEXT START ===" in last_user["content"]
    assert "=== CONTEXT END ===" in last_user["content"]


def test_docs_present_with_user_system_uses_rules_only_variant_at_index_0_user_system_at_index_1():
    from ragent.schemas.chat import _RAG_GROUNDING_RULES, _DEFAULT_RAG_SYSTEM_PROMPT

    doc = _doc("e", source_title="T", document_id="d", source_app="a")
    req = _req(
        {"role": "system", "content": "You are a pirate"},
        {"role": "user", "content": "q"},
    )
    result = build_rag_messages(req, [doc])

    assert result[0]["role"] == "system"
    assert result[0]["content"] == _RAG_GROUNDING_RULES
    assert result[1]["role"] == "system"
    assert result[1]["content"] == "You are a pirate"
    assert result[0]["content"] != _DEFAULT_RAG_SYSTEM_PROMPT


# --- docs present: user message wrapping ---


def test_wrapped_user_message_contains_context_markers_and_original_query_verbatim():
    doc = _doc("excerpt", source_title="T1", document_id="d1", source_app="app1")
    original_query = "Tell me about the project"
    req = _req({"role": "user", "content": original_query})
    result = build_rag_messages(req, [doc])

    last_user_content = result[-1]["content"]
    assert "=== CONTEXT START ===" in last_user_content
    assert "=== CONTEXT END ===" in last_user_content
    assert original_query in last_user_content
    ctx_end_pos = last_user_content.index("=== CONTEXT END ===")
    query_pos = last_user_content.index(original_query)
    assert query_pos > ctx_end_pos


def test_rendered_chunk_contains_source_app_source_title_document_id_and_excerpt():
    doc = _doc("The actual excerpt text", source_app="jira", source_title="Issue-42", document_id="DOC99")
    req = _req({"role": "user", "content": "q"})
    result = build_rag_messages(req, [doc])

    ctx_block = result[-1]["content"]
    assert "source_app=jira" in ctx_block
    assert "source_title=Issue-42" in ctx_block
    assert "document_id=DOC99" in ctx_block
    assert "The actual excerpt text" in ctx_block


def test_only_last_user_message_wrapped_earlier_user_messages_untouched():
    doc = _doc("e", source_title="T", document_id="d", source_app="a")
    req = _req(
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "follow-up"},
    )
    result = build_rag_messages(req, [doc])

    user_msgs = [m for m in result if m["role"] == "user"]
    assert len(user_msgs) == 2
    assert "=== CONTEXT START ===" not in user_msgs[0]["content"]
    assert "=== CONTEXT START ===" in user_msgs[1]["content"]
    assert "follow-up" in user_msgs[1]["content"]


# --- system template content ---


def test_default_system_template_contains_question_summary_generation_intent_blocks_and_dont_know_clause():
    from ragent.schemas.chat import _DEFAULT_RAG_SYSTEM_PROMPT

    assert "QUESTION" in _DEFAULT_RAG_SYSTEM_PROMPT
    assert "SUMMARY" in _DEFAULT_RAG_SYSTEM_PROMPT
    assert "GENERATION" in _DEFAULT_RAG_SYSTEM_PROMPT
    assert "I don't know based on the provided context" in _DEFAULT_RAG_SYSTEM_PROMPT


def test_default_system_template_contains_few_shot_examples_for_each_intent():
    from ragent.schemas.chat import _DEFAULT_RAG_SYSTEM_PROMPT

    assert _DEFAULT_RAG_SYSTEM_PROMPT.count("User:") >= 3
    assert _DEFAULT_RAG_SYSTEM_PROMPT.count("Assistant:") >= 3


# --- edge cases ---


def test_missing_meta_renders_unknown_without_raising():
    doc = SimpleNamespace(content="text", meta=None)
    req = _req({"role": "user", "content": "q"})
    result = build_rag_messages(req, [doc])

    ctx = result[-1]["content"]
    assert "source_app=unknown" in ctx
    assert "source_title=unknown" in ctx
    assert "document_id=unknown" in ctx


def test_env_var_override_via_importlib_reload():
    import ragent.schemas.chat as mod

    with patch.dict(
        "os.environ",
        {"RAGENT_DEFAULT_RAG_SYSTEM_PROMPT": "CUSTOM TEMPLATE WITHOUT PLACEHOLDER"},
    ):
        importlib.reload(mod)
        assert mod._DEFAULT_RAG_SYSTEM_PROMPT == "CUSTOM TEMPLATE WITHOUT PLACEHOLDER"

    importlib.reload(mod)  # restore
