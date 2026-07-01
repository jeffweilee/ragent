"""T-SK — MCP skill-management tools (list/get/update/delete): advertisement,
owner-scoping, fail-closed identity, and SkillService error mapping.

`create_skill` has its own suite (`test_mcp_create_skill.py`); this covers the
rest of the CRUD family that the `skill-manager` preset drives server-side.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragent.routers.mcp import create_mcp_router
from ragent.services.skill_service import (
    SkillNameConflictError,
    SkillNotFoundError,
    SkillReadOnlyError,
)

ALICE = {"X-User-Id": "alice"}


def _skill_resp(**over) -> SimpleNamespace:
    base = {
        "skill_id": "SKILL000000000000000000000",
        "name": "Translator",
        "description": "to English",
        "instructions": "Translate to English.",
        "enabled": True,
        "readonly": False,
        "created_at": "2026-06-24T00:00:00+00:00",
        "updated_at": "2026-06-24T00:00:00+00:00",
    }
    base.update(over)
    return SimpleNamespace(**base)


def _client(skill_service=None) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_mcp_router(retrieval_pipeline=MagicMock(), skill_service=skill_service)
    )
    return TestClient(app)


def _call(client: TestClient, name: str, arguments: dict, headers: dict | None = None) -> dict:
    resp = client.post(
        "/mcp/v1",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers=headers or {},
    )
    return resp.json()


# --- advertisement -----------------------------------------------------------


def test_tools_list_advertises_full_crud_family_when_wired():
    client = _client(skill_service=AsyncMock())
    resp = client.post(
        "/mcp/v1", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    )
    names = {t["name"] for t in resp.json()["result"]["tools"]}
    assert {
        "create_skill",
        "list_skills",
        "get_skill",
        "update_skill",
        "delete_skill",
    } <= names


def test_tools_list_omits_crud_family_without_skill_service():
    client = _client(skill_service=None)
    resp = client.post(
        "/mcp/v1", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    )
    names = {t["name"] for t in resp.json()["result"]["tools"]}
    assert names == {"retrieve"}


# --- list_skills -------------------------------------------------------------


def test_list_skills_returns_briefs_for_authenticated_owner():
    svc = AsyncMock()
    svc.list_for_user = AsyncMock(return_value=[_skill_resp(readonly=True), _skill_resp()])
    client = _client(skill_service=svc)
    body = _call(client, "list_skills", {}, headers=ALICE)
    skills = body["result"]["structuredContent"]["skills"]
    assert len(skills) == 2
    # brief shape — no instructions/timestamps leak into the list.
    assert set(skills[0]) == {"skill_id", "name", "description", "enabled", "readonly"}
    assert svc.list_for_user.call_args.kwargs["user_id"] == "alice"


def test_list_skills_without_identity_fails_closed():
    svc = AsyncMock()
    client = _client(skill_service=svc)
    body = _call(client, "list_skills", {})  # no X-User-Id
    assert body["error"]["data"]["error_code"] == "MISSING_USER_ID"
    svc.list_for_user.assert_not_called()


def test_list_skills_rejects_stray_arguments():
    svc = AsyncMock()
    client = _client(skill_service=svc)
    body = _call(client, "list_skills", {"user_id": "victim"}, headers=ALICE)
    assert body["error"]["data"]["error_code"] == "MCP_TOOL_INPUT_INVALID"
    svc.list_for_user.assert_not_called()


# --- get_skill ---------------------------------------------------------------


def test_get_skill_returns_full_object():
    svc = AsyncMock()
    svc.get = AsyncMock(return_value=_skill_resp())
    client = _client(skill_service=svc)
    body = _call(client, "get_skill", {"skill_id": "SKILL000000000000000000000"}, headers=ALICE)
    skill = body["result"]["structuredContent"]["skill"]
    assert skill["instructions"] == "Translate to English."
    assert skill["created_at"] == "2026-06-24T00:00:00+00:00"
    assert svc.get.call_args.kwargs == {
        "user_id": "alice",
        "skill_id": "SKILL000000000000000000000",
    }


def test_get_skill_not_found_maps_to_error_code():
    svc = AsyncMock()
    svc.get = AsyncMock(side_effect=SkillNotFoundError("nope"))
    client = _client(skill_service=svc)
    body = _call(client, "get_skill", {"skill_id": "SKILL000000000000000000000"}, headers=ALICE)
    assert body["error"]["data"]["error_code"] == "SKILL_NOT_FOUND"


def test_get_skill_without_identity_fails_closed():
    svc = AsyncMock()
    client = _client(skill_service=svc)
    body = _call(client, "get_skill", {"skill_id": "SKILL000000000000000000000"})
    assert body["error"]["data"]["error_code"] == "MISSING_USER_ID"
    svc.get.assert_not_called()


def test_get_skill_rejects_stray_arguments():
    svc = AsyncMock()
    client = _client(skill_service=svc)
    body = _call(
        client,
        "get_skill",
        {"skill_id": "SKILL000000000000000000000", "user_id": "victim"},
        headers=ALICE,
    )
    assert body["error"]["data"]["error_code"] == "MCP_TOOL_INPUT_INVALID"
    svc.get.assert_not_called()


# --- update_skill ------------------------------------------------------------


# A full-replace update must carry every write field — omitting one is a schema
# error, not a partial edit (guards against silently clobbering description/enabled).
_FULL_UPDATE = {
    "skill_id": "SKILL000000000000000000000",
    "name": "Renamed",
    "description": "still described",
    "instructions": "Do the new thing.",
    "enabled": False,
}


def test_update_skill_uses_authenticated_owner():
    svc = AsyncMock()
    svc.update = AsyncMock(return_value=_skill_resp(name="Renamed"))
    client = _client(skill_service=svc)
    body = _call(client, "update_skill", dict(_FULL_UPDATE), headers=ALICE)
    skill = body["result"]["structuredContent"]["skill"]
    assert skill["name"] == "Renamed"
    assert body["result"]["isError"] is False
    # every write field is forwarded verbatim — no defaulted description/enabled.
    assert svc.update.call_args.kwargs == {
        "user_id": "alice",
        "skill_id": "SKILL000000000000000000000",
        "name": "Renamed",
        "description": "still described",
        "instructions": "Do the new thing.",
        "enabled": False,
    }


def test_update_skill_readonly_preset_maps_to_error_code():
    svc = AsyncMock()
    svc.update = AsyncMock(side_effect=SkillReadOnlyError("built-in"))
    client = _client(skill_service=svc)
    body = _call(
        client, "update_skill", {**_FULL_UPDATE, "skill_id": "skill-manager"}, headers=ALICE
    )
    assert body["error"]["data"]["error_code"] == "SKILL_READONLY"


def test_update_skill_name_conflict_maps_to_error_code():
    svc = AsyncMock()
    svc.update = AsyncMock(side_effect=SkillNameConflictError("dup"))
    client = _client(skill_service=svc)
    body = _call(client, "update_skill", {**_FULL_UPDATE, "name": "dup"}, headers=ALICE)
    assert body["error"]["data"]["error_code"] == "SKILL_NAME_CONFLICT"


def test_update_skill_missing_required_field_is_invalid():
    svc = AsyncMock()
    client = _client(skill_service=svc)
    body = _call(
        client,
        "update_skill",
        {"skill_id": "SKILL000000000000000000000", "name": "x"},  # no instructions
        headers=ALICE,
    )
    assert body["error"]["data"]["error_code"] == "MCP_TOOL_INPUT_INVALID"
    svc.update.assert_not_called()


def test_update_skill_partial_body_is_invalid_not_a_silent_clobber():
    # Omitting description/enabled on a full-replace update must be rejected, not
    # defaulted — otherwise a rename would wipe the description / re-enable the skill.
    svc = AsyncMock()
    client = _client(skill_service=svc)
    body = _call(
        client,
        "update_skill",
        {
            "skill_id": "SKILL000000000000000000000",
            "name": "Renamed",
            "instructions": "Do the new thing.",
        },  # no description, no enabled
        headers=ALICE,
    )
    assert body["error"]["data"]["error_code"] == "MCP_TOOL_INPUT_INVALID"
    svc.update.assert_not_called()


def test_update_skill_not_found_maps_to_error_code():
    # A foreign/missing id must surface as SKILL_NOT_FOUND (never leak existence).
    svc = AsyncMock()
    svc.update = AsyncMock(side_effect=SkillNotFoundError("nope"))
    client = _client(skill_service=svc)
    body = _call(client, "update_skill", dict(_FULL_UPDATE), headers=ALICE)
    assert body["error"]["data"]["error_code"] == "SKILL_NOT_FOUND"


def test_update_skill_without_identity_fails_closed():
    svc = AsyncMock()
    client = _client(skill_service=svc)
    body = _call(client, "update_skill", dict(_FULL_UPDATE))
    assert body["error"]["data"]["error_code"] == "MISSING_USER_ID"
    svc.update.assert_not_called()


# --- delete_skill ------------------------------------------------------------


def test_delete_skill_reports_deleted():
    svc = AsyncMock()
    svc.delete = AsyncMock(return_value=None)
    client = _client(skill_service=svc)
    body = _call(client, "delete_skill", {"skill_id": "SKILL000000000000000000000"}, headers=ALICE)
    structured = body["result"]["structuredContent"]
    assert structured == {"skill_id": "SKILL000000000000000000000", "deleted": True}
    assert svc.delete.call_args.kwargs == {
        "user_id": "alice",
        "skill_id": "SKILL000000000000000000000",
    }


def test_delete_skill_readonly_preset_maps_to_error_code():
    svc = AsyncMock()
    svc.delete = AsyncMock(side_effect=SkillReadOnlyError("built-in"))
    client = _client(skill_service=svc)
    body = _call(client, "delete_skill", {"skill_id": "skill-manager"}, headers=ALICE)
    assert body["error"]["data"]["error_code"] == "SKILL_READONLY"


def test_delete_skill_not_found_maps_to_error_code():
    svc = AsyncMock()
    svc.delete = AsyncMock(side_effect=SkillNotFoundError("nope"))
    client = _client(skill_service=svc)
    body = _call(client, "delete_skill", {"skill_id": "SKILL000000000000000000000"}, headers=ALICE)
    assert body["error"]["data"]["error_code"] == "SKILL_NOT_FOUND"


def test_delete_skill_without_identity_fails_closed():
    svc = AsyncMock()
    client = _client(skill_service=svc)
    body = _call(client, "delete_skill", {"skill_id": "SKILL000000000000000000000"})
    assert body["error"]["data"]["error_code"] == "MISSING_USER_ID"
    svc.delete.assert_not_called()


def test_delete_skill_rejects_stray_arguments():
    svc = AsyncMock()
    client = _client(skill_service=svc)
    body = _call(
        client,
        "delete_skill",
        {"skill_id": "SKILL000000000000000000000", "user_id": "victim"},
        headers=ALICE,
    )
    assert body["error"]["data"]["error_code"] == "MCP_TOOL_INPUT_INVALID"
    svc.delete.assert_not_called()


def test_skill_tool_unexpected_error_wrapped_as_execution_failed():
    # A non-domain backend failure surfaces as a JSON-RPC envelope, not a 500.
    svc = AsyncMock()
    svc.list_for_user = AsyncMock(side_effect=RuntimeError("db down"))
    client = _client(skill_service=svc)
    body = _call(client, "list_skills", {}, headers=ALICE)
    assert body["error"]["data"]["error_code"] == "MCP_TOOL_EXECUTION_FAILED"
