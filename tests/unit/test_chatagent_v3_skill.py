"""T-SK — /chatagent/v3 applies an owner-scoped skill as machine-context.

The skill_id rides in forwardedProps. The router resolves it (owner-scoped) and
appends a ContextItem so the existing caller path wraps it into the <hidden>
machine-context block — no new upstream field, and the v3 session-read strips it
from the rendered history. A missing/foreign/disabled skill becomes a RUN_ERROR
over the 200 stream (v3 never returns an HTTP 4xx).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragent.routers.chatagent_v3 import create_chatagent_v3_router
from ragent.services.skill_service import SkillNotFoundError


class _CapturingAgent:
    def __init__(self) -> None:
        self.captured = None

    def run(self, request, model):
        self.captured = request
        yield "data: [DONE]\n\n"


def _build(skill_service):
    agent = _CapturingAgent()
    app = FastAPI()
    app.include_router(
        create_chatagent_v3_router(
            http_client=MagicMock(spec=httpx.Client),
            chatagent_ap_name="TestAP",
            chatagent_api_url="http://upstream",
            agent_factory=lambda user_id, token, attachments_block: agent,
            skill_service=skill_service,
        )
    )
    return app, agent


def _run_input(forwarded_props=None) -> dict:
    return {
        "threadId": "thread_1",
        "runId": "run_1",
        "messages": [{"id": "m1", "role": "user", "content": "Hi"}],
        "tools": [],
        "state": None,
        "context": [],
        "forwardedProps": forwarded_props,
    }


def test_skill_id_injects_instructions_as_context_item():
    svc = AsyncMock()
    svc.resolve_instructions = AsyncMock(return_value="Always answer like a pirate.")
    app, agent = _build(svc)
    with TestClient(app) as client:
        r = client.post(
            "/chatagent/v3",
            json=_run_input({"skillId": "SKILL000000000000000000000"}),
            headers={"X-User-Id": "alice"},
        )
    assert r.status_code == 200
    # resolved owner-scoped, and the instructions are appended as context.
    assert svc.resolve_instructions.call_args.kwargs == {
        "user_id": "alice",
        "skill_id": "SKILL000000000000000000000",
    }
    values = [c.value for c in agent.captured.context]
    assert "Always answer like a pirate." in values


def test_no_skill_id_leaves_context_untouched():
    svc = AsyncMock()
    app, agent = _build(svc)
    with TestClient(app) as client:
        r = client.post("/chatagent/v3", json=_run_input(None), headers={"X-User-Id": "alice"})
    assert r.status_code == 200
    assert agent.captured.context == []
    svc.resolve_instructions.assert_not_called()


def test_missing_or_foreign_skill_yields_run_error_and_skips_agent():
    svc = AsyncMock()
    svc.resolve_instructions = AsyncMock(side_effect=SkillNotFoundError("nope"))
    app, agent = _build(svc)
    with TestClient(app) as client:
        r = client.post(
            "/chatagent/v3",
            json=_run_input({"skillId": "SKILLofBOB"}),
            headers={"X-User-Id": "alice"},
        )
    assert r.status_code == 200
    assert "SKILL_NOT_FOUND" in r.text
    assert agent.captured is None  # upstream never reached on an invalid skill ref
