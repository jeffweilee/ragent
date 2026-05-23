"""T3.17 — build_rag_messages: context injection into user message + system prompt routing."""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import patch

from ragent.schemas.chat import ChatRequest, build_rag_messages


def _req(*messages: dict) -> ChatRequest:
    return ChatRequest(messages=list(messages))


def _doc(content: str = "excerpt text", **meta) -> SimpleNamespace:
    return SimpleNamespace(content=content, meta=meta)


# --- no docs ---


def test_no_docs_no_user_system_still_uses_rag_system_prompt():
    """Even with no docs, build_rag_messages must prepend the RAG system prompt — not fall back
    to the generic default — so the RAG boundary is always enforced."""
    from ragent.schemas.chat import _DEFAULT_RAG_SYSTEM_PROMPT

    req = _req({"role": "user", "content": "hello"})
    for docs in (None, []):
        result = build_rag_messages(req, docs)
        assert result[0]["role"] == "system"
        assert result[0]["content"] == _DEFAULT_RAG_SYSTEM_PROMPT


def test_no_docs_with_user_system_still_uses_grounding_rules():
    """Even with no docs and a caller-supplied system message, grounding rules must be prepended
    — the RAG boundary must not be removed when the context happens to be empty."""
    from ragent.schemas.chat import _RAG_GROUNDING_RULES

    req = _req(
        {"role": "system", "content": "Custom persona"},
        {"role": "user", "content": "hello"},
    )
    for docs in (None, []):
        result = build_rag_messages(req, docs)
        assert result[0]["content"] == _RAG_GROUNDING_RULES


def test_empty_docs_injects_empty_context_placeholder():
    """None and [] both → '(The context is empty.)' injected into the last user message."""
    req = _req({"role": "user", "content": "Q"})
    for docs in (None, []):
        result = build_rag_messages(req, docs)
        last_user = next(m for m in reversed(result) if m["role"] == "user")
        assert "(The context is empty.)" in last_user["content"]


# --- docs present: system prompt routing ---


def test_docs_present_prepends_rag_system_at_index_0_and_wraps_last_user():
    doc = _doc("some excerpt", source_title="Wiki", document_id="d1", source_app="confluence")
    req = _req({"role": "user", "content": "What is X?"})
    result = build_rag_messages(req, [doc])

    assert result[0]["role"] == "system"
    last_user = result[-1]
    assert last_user["role"] == "user"
    assert "<context>" in last_user["content"]
    assert "</context>" in last_user["content"]


def test_docs_present_with_user_system_uses_rules_only_variant_at_index_0_user_system_at_index_1():
    from ragent.schemas.chat import _RAG_GROUNDING_RULES

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


# --- docs present: user message wrapping ---


def test_wrapped_user_message_contains_context_markers_and_original_query_verbatim():
    doc = _doc("excerpt", source_title="T1", document_id="d1", source_app="app1")
    original_query = "Tell me about the project"
    req = _req({"role": "user", "content": original_query})
    result = build_rag_messages(req, [doc])

    last_user_content = result[-1]["content"]
    assert "<context>" in last_user_content
    assert "</context>" in last_user_content
    assert original_query in last_user_content
    ctx_end_pos = last_user_content.index("</context>")
    query_pos = last_user_content.index(original_query)
    assert query_pos > ctx_end_pos


def test_context_block_uses_xml_tags_not_equals_markers():
    doc = _doc("body", source_title="T", document_id="d", source_app="a")
    req = _req({"role": "user", "content": "Q"})
    result = build_rag_messages(req, [doc])

    last_user = next(m for m in reversed(result) if m["role"] == "user")
    assert "<context>" in last_user["content"]
    assert "</context>" in last_user["content"]
    assert "=== CONTEXT START ===" not in last_user["content"]
    assert "=== CONTEXT END ===" not in last_user["content"]


def test_rendered_chunk_contains_source_index_and_excerpt():
    """Context renders [資料來源 #N] index + excerpt body; raw metadata is hidden from the model."""
    doc = _doc(
        "The actual excerpt text", source_app="jira", source_title="Issue-42", document_id="DOC99"
    )
    req = _req({"role": "user", "content": "q"})
    result = build_rag_messages(req, [doc])

    ctx_block = result[-1]["content"]
    assert "[資料來源 #1]" in ctx_block
    assert "The actual excerpt text" in ctx_block
    # Raw metadata fields are hidden from the model
    assert "source_app=jira" not in ctx_block
    assert "document_id=DOC99" not in ctx_block


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
    assert "<context>" not in user_msgs[0]["content"]
    assert "<context>" in user_msgs[1]["content"]
    assert "follow-up" in user_msgs[1]["content"]


# --- system template content ---


def test_default_system_template_contains_intent_blocks_and_empathetic_refusal():
    from ragent.schemas.chat import _DEFAULT_RAG_SYSTEM_PROMPT

    assert "QUESTION" in _DEFAULT_RAG_SYSTEM_PROMPT
    assert "SUMMARY" in _DEFAULT_RAG_SYSTEM_PROMPT
    assert "GENERATION" in _DEFAULT_RAG_SYSTEM_PROMPT
    # Empathetic refusal in 4 languages replaces the mechanical "I don't know" phrase
    assert "我理解您的問題" in _DEFAULT_RAG_SYSTEM_PROMPT
    assert "I understand your question" in _DEFAULT_RAG_SYSTEM_PROMPT


def test_default_system_template_contains_few_shot_example():
    from ragent.schemas.chat import _DEFAULT_RAG_SYSTEM_PROMPT

    # At least one User/Assistant example exists (previously required ≥3 per intent;
    # new design consolidates to one illustrative QUESTION example)
    assert _DEFAULT_RAG_SYSTEM_PROMPT.count("User:") >= 1
    assert _DEFAULT_RAG_SYSTEM_PROMPT.count("Assistant:") >= 1


def test_system_prompt_contains_chitchat_rule():
    from ragent.schemas.chat import _DEFAULT_RAG_SYSTEM_PROMPT, _RAG_GROUNDING_RULES

    for prompt in (_DEFAULT_RAG_SYSTEM_PROMPT, _RAG_GROUNDING_RULES):
        assert "CHITCHAT" in prompt


def test_system_prompt_contains_language_mirroring_rule():
    from ragent.schemas.chat import _DEFAULT_RAG_SYSTEM_PROMPT, _RAG_GROUNDING_RULES

    for prompt in (_DEFAULT_RAG_SYSTEM_PROMPT, _RAG_GROUNDING_RULES):
        assert "LANGUAGE MIRRORING" in prompt


def test_system_prompt_contains_empathetic_refusal_in_four_languages():
    from ragent.schemas.chat import _DEFAULT_RAG_SYSTEM_PROMPT

    assert "我理解您的問題" in _DEFAULT_RAG_SYSTEM_PROMPT  # Traditional Chinese
    assert "我理解您的问题" in _DEFAULT_RAG_SYSTEM_PROMPT  # Simplified Chinese
    assert "I understand your question" in _DEFAULT_RAG_SYSTEM_PROMPT  # English
    assert "ご質問" in _DEFAULT_RAG_SYSTEM_PROMPT  # Japanese


def test_system_prompt_citation_bans_wrong_formats():
    from ragent.schemas.chat import _DEFAULT_RAG_SYSTEM_PROMPT, _RAG_GROUNDING_RULES

    for prompt in (_DEFAULT_RAG_SYSTEM_PROMPT, _RAG_GROUNDING_RULES):
        # The ban must be explicit — the prompt must mention the forbidden format
        assert "【" in prompt
        # And mandate the correct numeric-only format
        assert "[1]" in prompt


def test_system_prompt_bans_context_tag_echo():
    from ragent.schemas.chat import _DEFAULT_RAG_SYSTEM_PROMPT, _RAG_GROUNDING_RULES

    for prompt in (_DEFAULT_RAG_SYSTEM_PROMPT, _RAG_GROUNDING_RULES):
        assert "STRUCTURE GUARD" in prompt


# --- edge cases ---


def test_missing_meta_renders_without_raising():
    """Docs with meta=None must not raise; index label is still emitted."""
    doc = SimpleNamespace(content="text", meta=None)
    req = _req({"role": "user", "content": "q"})
    result = build_rag_messages(req, [doc])

    ctx = result[-1]["content"]
    assert "[資料來源 #1]" in ctx


def test_env_var_override_via_importlib_reload():
    import ragent.schemas.chat as mod

    with patch.dict(
        "os.environ",
        {"RAGENT_DEFAULT_RAG_SYSTEM_PROMPT": "CUSTOM TEMPLATE WITHOUT PLACEHOLDER"},
    ):
        importlib.reload(mod)
        assert mod._DEFAULT_RAG_SYSTEM_PROMPT == "CUSTOM TEMPLATE WITHOUT PLACEHOLDER"

    importlib.reload(mod)  # restore
