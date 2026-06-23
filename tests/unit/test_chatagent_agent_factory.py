"""T-CAv3.DIP — composition root's chatagent agent-factory builder."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
from twp_ai.agents.adk import ADKAgent

from ragent.bootstrap.composition import _build_chatagent_agent_factory


def test_build_chatagent_agent_factory_returns_callable_producing_an_agent() -> None:
    factory = _build_chatagent_agent_factory(
        MagicMock(spec=httpx.Client),
        api_url="http://upstream",
        ap_name="TestAP",
        auth="Bearer up",
        timeout=5.0,
    )

    agent = factory("alice", "token-123")

    assert isinstance(agent, ADKAgent)
    assert callable(agent.run)


def test_build_chatagent_agent_factory_binds_per_request_user_and_token() -> None:
    # Each call must produce an independent Agent bound to that call's
    # user_id/user_token (ADKCaller carries per-request state, so the factory
    # must not return a cached singleton).
    factory = _build_chatagent_agent_factory(
        MagicMock(spec=httpx.Client),
        api_url="http://upstream",
        ap_name="TestAP",
        auth=None,
        timeout=5.0,
    )

    agent_a = factory("alice", "token-a")
    agent_b = factory("bob", "token-b")

    assert agent_a is not agent_b
    assert agent_a._caller._user_id == "alice"  # noqa: SLF001
    assert agent_b._caller._user_id == "bob"  # noqa: SLF001
