"""T3.3 — ChatRequest schema: validation, env defaults (B12, S6b, S6c, S6i)."""

import pytest
from pydantic import ValidationError


def _req(**kwargs):
    from ragent.schemas.chat import ChatRequest

    return ChatRequest(**kwargs)


def test_messages_required_missing():
    from ragent.schemas.chat import ChatRequest

    with pytest.raises(ValidationError) as exc_info:
        ChatRequest()
    errors = exc_info.value.errors()
    assert any(e["loc"] == ("messages",) for e in errors)


def test_messages_required_empty():
    from ragent.schemas.chat import ChatRequest

    with pytest.raises(ValidationError) as exc_info:
        ChatRequest(messages=[])
    errors = exc_info.value.errors()
    assert any("messages" in str(e["loc"]) for e in errors)


def test_defaults_from_env(monkeypatch):
    monkeypatch.setenv("RAGENT_DEFAULT_LLM_PROVIDER", "openai")
    monkeypatch.setenv("RAGENT_DEFAULT_LLM_MODEL", "gptoss-120b")
    monkeypatch.setenv("RAGENT_DEFAULT_TEMPERATURE", "0.7")
    monkeypatch.setenv("RAGENT_DEFAULT_MAX_TOKENS", "4096")
    req = _req(messages=[{"role": "user", "content": "hi"}])
    assert req.provider == "openai"
    assert req.model == "gptoss-120b"
    assert req.temperature == 0.7
    assert req.max_tokens == 4096


def test_provider_must_be_in_allowlist():
    from ragent.schemas.chat import ChatRequest

    with pytest.raises(ValidationError) as exc_info:
        ChatRequest(messages=[{"role": "user", "content": "hi"}], provider="anthropic")
    errors = exc_info.value.errors()
    assert any("provider" in str(e["loc"]) for e in errors)


def test_provider_openai_accepted():
    req = _req(messages=[{"role": "user", "content": "hi"}], provider="openai")
    assert req.provider == "openai"


def test_source_app_and_workspace_optional():
    req = _req(messages=[{"role": "user", "content": "hi"}])
    assert req.source_app is None
    assert req.source_meta is None


def test_source_app_empty_string_rejected():
    from ragent.schemas.chat import ChatRequest

    with pytest.raises(ValidationError) as exc_info:
        ChatRequest(messages=[{"role": "user", "content": "hi"}], source_app="")
    errors = exc_info.value.errors()
    assert any("source_app" in str(e["loc"]) for e in errors)


def test_source_app_too_long_rejected():
    from ragent.schemas.chat import ChatRequest

    with pytest.raises(ValidationError) as exc_info:
        ChatRequest(messages=[{"role": "user", "content": "hi"}], source_app="x" * 65)
    errors = exc_info.value.errors()
    assert any("source_app" in str(e["loc"]) for e in errors)


def test_source_meta_empty_string_rejected():
    from ragent.schemas.chat import ChatRequest

    with pytest.raises(ValidationError) as exc_info:
        ChatRequest(messages=[{"role": "user", "content": "hi"}], source_meta="")
    errors = exc_info.value.errors()
    assert any("source_meta" in str(e["loc"]) for e in errors)


def test_rag_system_prompt_enforces_markdown():
    """RAG-grounded chat LLM output must be formatted as Markdown."""
    from types import SimpleNamespace

    from ragent.schemas.chat import ChatRequest, build_rag_messages

    req = ChatRequest(messages=[{"role": "user", "content": "hi"}])
    docs = [SimpleNamespace(content="ctx", meta={"source_title": "S"})]
    msgs = build_rag_messages(req, docs)
    assert msgs[0]["role"] == "system"
    assert "markdown" in msgs[0]["content"].lower()


def test_rag_grounding_rules_enforces_markdown():
    """When caller provides a system prompt, grounding rules still mandate Markdown."""
    from types import SimpleNamespace

    from ragent.schemas.chat import ChatRequest, build_rag_messages

    req = ChatRequest(
        messages=[
            {"role": "system", "content": "custom"},
            {"role": "user", "content": "hi"},
        ]
    )
    docs = [SimpleNamespace(content="ctx", meta={"source_title": "S"})]
    msgs = build_rag_messages(req, docs)
    assert msgs[0]["role"] == "system"
    assert "markdown" in msgs[0]["content"].lower()


def test_source_meta_too_long_rejected():
    from ragent.schemas.chat import ChatRequest

    with pytest.raises(ValidationError) as exc_info:
        ChatRequest(messages=[{"role": "user", "content": "hi"}], source_meta="y" * 1025)
    errors = exc_info.value.errors()
    assert any("source_meta" in str(e["loc"]) for e in errors)


def test_source_meta_long_value_accepted():
    req = _req(messages=[{"role": "user", "content": "hi"}], source_meta="y" * 1024)
    assert req.source_meta == "y" * 1024


# ---------------------------------------------------------------------------
# top_k and min_score fields
# ---------------------------------------------------------------------------


def test_chat_request_top_k_defaults_to_DEFAULT_TOP_K():
    from ragent.pipelines.retrieve import DEFAULT_TOP_K

    req = _req(messages=[{"role": "user", "content": "hi"}])
    assert req.top_k == DEFAULT_TOP_K


def test_chat_request_top_k_accepts_explicit_value():
    req = _req(messages=[{"role": "user", "content": "hi"}], top_k=5)
    assert req.top_k == 5


def test_chat_request_top_k_must_be_at_least_one():
    from ragent.schemas.chat import ChatRequest

    with pytest.raises(ValidationError) as exc:
        ChatRequest(messages=[{"role": "user", "content": "hi"}], top_k=0)
    assert any("top_k" in str(e["loc"]) for e in exc.value.errors())


def test_chat_request_top_k_capped_at_200():
    from ragent.schemas.chat import ChatRequest

    with pytest.raises(ValidationError) as exc:
        ChatRequest(messages=[{"role": "user", "content": "hi"}], top_k=201)
    assert any("top_k" in str(e["loc"]) for e in exc.value.errors())


def test_chat_request_min_score_defaults_to_DEFAULT_MIN_SCORE():
    from ragent.pipelines.retrieve import DEFAULT_MIN_SCORE

    req = _req(messages=[{"role": "user", "content": "hi"}])
    assert req.min_score == DEFAULT_MIN_SCORE


def test_chat_request_min_score_accepts_explicit_value():
    req = _req(messages=[{"role": "user", "content": "hi"}], min_score=0.5)
    assert req.min_score == pytest.approx(0.5)


def test_chat_request_min_score_accepts_zero():
    req = _req(messages=[{"role": "user", "content": "hi"}], min_score=0.0)
    assert req.min_score == pytest.approx(0.0)


def test_chat_request_min_score_must_be_non_negative():
    from ragent.schemas.chat import ChatRequest

    with pytest.raises(ValidationError) as exc:
        ChatRequest(messages=[{"role": "user", "content": "hi"}], min_score=-0.1)
    assert any("min_score" in str(e["loc"]) for e in exc.value.errors())


def test_chat_request_min_score_accepts_none_explicitly():
    req = _req(messages=[{"role": "user", "content": "hi"}], min_score=None)
    assert req.min_score is None
