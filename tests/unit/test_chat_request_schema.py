"""T3.3 — ChatRequest schema: validation, env defaults, normalize_messages (B12, S6b, S6c, S6i)."""

import pytest
from pydantic import ValidationError


def _import():
    from ragent.schemas.chat import ChatRequest, normalize_messages

    return ChatRequest, normalize_messages


def test_messages_required_missing():
    ChatRequest, _ = _import()
    with pytest.raises(ValidationError) as exc_info:
        ChatRequest()
    errors = exc_info.value.errors()
    assert any(e["loc"] == ("messages",) for e in errors)


def test_messages_required_empty():
    ChatRequest, _ = _import()
    with pytest.raises(ValidationError) as exc_info:
        ChatRequest(messages=[])
    errors = exc_info.value.errors()
    assert any("messages" in str(e["loc"]) for e in errors)


def test_defaults_from_env(monkeypatch):
    ChatRequest, _ = _import()
    monkeypatch.setenv("RAGENT_DEFAULT_LLM_PROVIDER", "openai")
    monkeypatch.setenv("RAGENT_DEFAULT_LLM_MODEL", "gptoss-120b")
    monkeypatch.setenv("RAGENT_DEFAULT_TEMPERATURE", "0.7")
    monkeypatch.setenv("RAGENT_DEFAULT_MAX_TOKENS", "4096")
    req = ChatRequest(messages=[{"role": "user", "content": "hi"}])
    assert req.provider == "openai"
    assert req.model == "gptoss-120b"
    assert req.temperature == 0.7
    assert req.max_tokens == 4096


def test_provider_must_be_in_allowlist():
    ChatRequest, _ = _import()
    with pytest.raises(ValidationError) as exc_info:
        ChatRequest(messages=[{"role": "user", "content": "hi"}], provider="anthropic")
    errors = exc_info.value.errors()
    assert any("provider" in str(e["loc"]) for e in errors)


def test_provider_openai_accepted():
    ChatRequest, _ = _import()
    req = ChatRequest(messages=[{"role": "user", "content": "hi"}], provider="openai")
    assert req.provider == "openai"


def test_normalize_prepends_system_when_absent():
    ChatRequest, normalize_messages = _import()
    req = ChatRequest(messages=[{"role": "user", "content": "hello"}])
    msgs = normalize_messages(req)
    assert msgs[0]["role"] == "system"
    assert len(msgs) == 2


def test_normalize_does_not_prepend_when_system_present():
    ChatRequest, normalize_messages = _import()
    req = ChatRequest(
        messages=[
            {"role": "system", "content": "custom"},
            {"role": "user", "content": "hello"},
        ]
    )
    msgs = normalize_messages(req)
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"] == "custom"
    assert len(msgs) == 2


def test_source_app_and_workspace_optional():
    ChatRequest, _ = _import()
    req = ChatRequest(messages=[{"role": "user", "content": "hi"}])
    assert req.source_app is None
    assert req.source_workspace is None


def test_source_app_empty_string_rejected():
    ChatRequest, _ = _import()
    with pytest.raises(ValidationError) as exc_info:
        ChatRequest(messages=[{"role": "user", "content": "hi"}], source_app="")
    errors = exc_info.value.errors()
    assert any("source_app" in str(e["loc"]) for e in errors)


def test_source_app_too_long_rejected():
    ChatRequest, _ = _import()
    with pytest.raises(ValidationError) as exc_info:
        ChatRequest(messages=[{"role": "user", "content": "hi"}], source_app="x" * 65)
    errors = exc_info.value.errors()
    assert any("source_app" in str(e["loc"]) for e in errors)


def test_source_workspace_empty_string_rejected():
    ChatRequest, _ = _import()
    with pytest.raises(ValidationError) as exc_info:
        ChatRequest(messages=[{"role": "user", "content": "hi"}], source_workspace="")
    errors = exc_info.value.errors()
    assert any("source_workspace" in str(e["loc"]) for e in errors)


def test_source_workspace_too_long_rejected():
    ChatRequest, _ = _import()
    with pytest.raises(ValidationError) as exc_info:
        ChatRequest(messages=[{"role": "user", "content": "hi"}], source_workspace="y" * 65)
    errors = exc_info.value.errors()
    assert any("source_workspace" in str(e["loc"]) for e in errors)
