"""T-CA.S1 — ChatAgentRequest schema unit tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError


def test_session_defaults_none():
    from ragent.schemas.chatagent import ChatAgentRequest

    req = ChatAgentRequest(messages=[{"role": "user", "content": "hi"}])
    assert req.session is None


def test_session_passed_through():
    from ragent.schemas.chatagent import ChatAgentRequest

    req = ChatAgentRequest(messages=[{"role": "user", "content": "hi"}], session="my-session-id")
    assert req.session == "my-session-id"


def test_inherits_messages_required():
    from ragent.schemas.chatagent import ChatAgentRequest

    with pytest.raises(ValidationError):
        ChatAgentRequest()


def test_inherits_messages_min_length():
    from ragent.schemas.chatagent import ChatAgentRequest

    with pytest.raises(ValidationError):
        ChatAgentRequest(messages=[])


def test_inherits_provider_allowlist():
    from ragent.schemas.chatagent import ChatAgentRequest

    with pytest.raises(ValidationError):
        ChatAgentRequest(
            messages=[{"role": "user", "content": "hi"}],
            provider="anthropic",
        )
