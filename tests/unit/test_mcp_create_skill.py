"""T-SK — MCP `create_skill` write tool: advertisement, owner-scoping, errors."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragent.routers.mcp import create_mcp_router
from ragent.services.skill_service import SkillNameConflictError

ALICE = {"X-User-Id": "alice"}


def _skill_resp(**over) -> SimpleNamespace:
    base = {
        "skill_id": "SKILL000000000000000000000",
        "name": "Translator",
        "description": "to English",
        "enabled": True,
    }
    base.update(over)
    return SimpleNamespace(**base)


def _client(skill_service=None) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_mcp_router(retrieval_pipeline=MagicMock(), skill_service=skill_service)
    )
    return TestClient(app)


def _call(client: TestClient, arguments: dict, headers: dict | None = None) -> dict:
    resp = client.post(
        "/mcp/v1",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "create_skill", "arguments": arguments},
        },
        headers=headers or {},
    )
    return resp.json()


def test_tools_list_advertises_create_skill_when_wired():
    client = _client(skill_service=AsyncMock())
    resp = client.post(
        "/mcp/v1", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    )
    names = {t["name"] for t in resp.json()["result"]["tools"]}
    assert "create_skill" in names
    assert "retrieve" in names


def test_tools_list_omits_create_skill_without_skill_service():
    client = _client(skill_service=None)
    resp = client.post(
        "/mcp/v1", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    )
    names = {t["name"] for t in resp.json()["result"]["tools"]}
    assert "create_skill" not in names
    assert "retrieve" in names


def test_create_skill_uses_authenticated_owner():
    svc = AsyncMock()
    svc.create = AsyncMock(return_value=_skill_resp())
    client = _client(skill_service=svc)
    body = _call(
        client,
        {"name": "Translator", "description": "to English", "instructions": "Translate."},
        headers=ALICE,
    )
    assert body["result"]["structuredContent"]["skill"]["skill_id"] == "SKILL000000000000000000000"
    assert body["result"]["isError"] is False
    # owner is the authenticated caller, never a tool argument.
    assert svc.create.call_args.kwargs["user_id"] == "alice"


def test_create_skill_without_identity_fails_closed():
    svc = AsyncMock()
    client = _client(skill_service=svc)
    body = _call(client, {"name": "X", "instructions": "y"})  # no X-User-Id
    assert body["error"]["data"]["error_code"] == "MISSING_USER_ID"
    svc.create.assert_not_called()


def test_create_skill_ignores_user_id_in_arguments():
    # additionalProperties:false → a stray user_id arg is rejected, not honoured.
    svc = AsyncMock()
    svc.create = AsyncMock(return_value=_skill_resp())
    client = _client(skill_service=svc)
    body = _call(
        client,
        {"name": "X", "instructions": "y", "user_id": "victim"},
        headers=ALICE,
    )
    assert body["error"]["data"]["error_code"] == "MCP_TOOL_INPUT_INVALID"
    svc.create.assert_not_called()


def test_create_skill_conflict_surfaces_error():
    svc = AsyncMock()
    svc.create = AsyncMock(side_effect=SkillNameConflictError("dup"))
    client = _client(skill_service=svc)
    body = _call(client, {"name": "dup", "instructions": "y"}, headers=ALICE)
    assert body["error"]["data"]["error_code"] == "SKILL_NAME_CONFLICT"


def test_create_skill_missing_required_field_is_invalid():
    svc = AsyncMock()
    svc.create = AsyncMock()
    client = _client(skill_service=svc)
    body = _call(client, {"description": "no name or instructions"}, headers=ALICE)
    assert body["error"]["data"]["error_code"] == "MCP_TOOL_INPUT_INVALID"
    svc.create.assert_not_called()


def test_non_dict_arguments_rejected():
    # A falsy non-object ([]) must be rejected, not silently coerced to {}.
    svc = AsyncMock()
    svc.create = AsyncMock()
    client = _client(skill_service=svc)
    resp = client.post(
        "/mcp/v1",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "create_skill", "arguments": []},
        },
        headers=ALICE,
    )
    assert resp.json()["error"]["data"]["error_code"] == "MCP_TOOL_INPUT_INVALID"
    svc.create.assert_not_called()


def test_create_skill_unexpected_error_wrapped_as_execution_failed():
    # A non-conflict backend failure surfaces as a JSON-RPC envelope, not a 500.
    svc = AsyncMock()
    svc.create = AsyncMock(side_effect=RuntimeError("db down"))
    client = _client(skill_service=svc)
    body = _call(client, {"name": "X", "instructions": "y"}, headers=ALICE)
    assert body["error"]["data"]["error_code"] == "MCP_TOOL_EXECUTION_FAILED"
