"""T-SK — /skills/v1 router: owner-scoping, status codes, error_code shapes."""

from __future__ import annotations

from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragent.routers.skill import create_skill_router
from ragent.schemas.skill import SkillResponse
from ragent.services.skill_service import SkillNameConflictError, SkillNotFoundError

ALICE = {"X-User-Id": "alice"}


def _resp(**over) -> SkillResponse:
    base = {
        "skill_id": "SKILL000000000000000000000",
        "name": "Translator",
        "description": "",
        "instructions": "Translate to English.",
        "enabled": True,
        "created_at": "2026-06-24T00:00:00+00:00",
        "updated_at": "2026-06-24T00:00:00+00:00",
    }
    base.update(over)
    return SkillResponse(**base)


def _client(service) -> TestClient:
    app = FastAPI()
    app.include_router(create_skill_router(skill_service=service))
    return TestClient(app)


def test_create_returns_201_and_passes_resolved_owner():
    svc = AsyncMock()
    svc.create = AsyncMock(return_value=_resp())
    client = _client(svc)
    r = client.post(
        "/skills/v1",
        json={"name": "Translator", "instructions": "Translate to English."},
        headers=ALICE,
    )
    assert r.status_code == 201
    assert r.json()["skill_id"] == "SKILL000000000000000000000"
    # owner is the resolved header identity, never a body field.
    assert svc.create.call_args.kwargs["user_id"] == "alice"


def test_create_validation_error_uses_skill_validation_code():
    svc = AsyncMock()
    client = _client(svc)
    r = client.post("/skills/v1", json={"instructions": "x"}, headers=ALICE)  # missing name
    assert r.status_code == 422
    assert r.json()["error_code"] == "SKILL_VALIDATION"


def test_create_conflict_returns_409():
    svc = AsyncMock()
    svc.create = AsyncMock(side_effect=SkillNameConflictError("dup"))
    client = _client(svc)
    r = client.post("/skills/v1", json={"name": "dup", "instructions": "x"}, headers=ALICE)
    assert r.status_code == 409
    assert r.json()["error_code"] == "SKILL_NAME_CONFLICT"


def test_create_without_user_id_returns_422_missing_user():
    svc = AsyncMock()
    client = _client(svc)
    r = client.post("/skills/v1", json={"name": "n", "instructions": "x"})  # no header
    assert r.status_code == 422
    assert r.json()["error_code"] == "MISSING_USER_ID"
    svc.create.assert_not_called()


def test_list_returns_owner_skills():
    svc = AsyncMock()
    svc.list_for_user = AsyncMock(return_value=[_resp(), _resp(skill_id="SKILL1")])
    client = _client(svc)
    r = client.get("/skills/v1", headers=ALICE)
    assert r.status_code == 200
    assert len(r.json()["skills"]) == 2
    assert svc.list_for_user.call_args.kwargs["user_id"] == "alice"


def test_get_item_passes_owner_and_returns_200():
    svc = AsyncMock()
    svc.get = AsyncMock(return_value=_resp())
    client = _client(svc)
    r = client.get("/skills/v1/SKILL000000000000000000000", headers=ALICE)
    assert r.status_code == 200
    assert svc.get.call_args.kwargs == {
        "user_id": "alice",
        "skill_id": "SKILL000000000000000000000",
    }


def test_get_foreign_or_missing_returns_404():
    # The service raises NotFound for a skill the caller does not own — the
    # router surfaces it as 404 SKILL_NOT_FOUND (cross-user reads are invisible).
    svc = AsyncMock()
    svc.get = AsyncMock(side_effect=SkillNotFoundError("nope"))
    client = _client(svc)
    r = client.get("/skills/v1/SKILLofBOB", headers=ALICE)
    assert r.status_code == 404
    assert r.json()["error_code"] == "SKILL_NOT_FOUND"


def test_update_returns_200():
    svc = AsyncMock()
    svc.update = AsyncMock(return_value=_resp(name="New"))
    client = _client(svc)
    r = client.put(
        "/skills/v1/SKILL000000000000000000000",
        json={"name": "New", "instructions": "x"},
        headers=ALICE,
    )
    assert r.status_code == 200
    assert r.json()["name"] == "New"
    assert svc.update.call_args.kwargs["user_id"] == "alice"


def test_update_missing_returns_404():
    svc = AsyncMock()
    svc.update = AsyncMock(side_effect=SkillNotFoundError("nope"))
    client = _client(svc)
    r = client.put("/skills/v1/x", json={"name": "n", "instructions": "x"}, headers=ALICE)
    assert r.status_code == 404


def test_update_conflict_returns_409():
    svc = AsyncMock()
    svc.update = AsyncMock(side_effect=SkillNameConflictError("dup"))
    client = _client(svc)
    r = client.put("/skills/v1/x", json={"name": "dup", "instructions": "x"}, headers=ALICE)
    assert r.status_code == 409
    assert r.json()["error_code"] == "SKILL_NAME_CONFLICT"


def test_delete_returns_204():
    svc = AsyncMock()
    svc.delete = AsyncMock(return_value=None)
    client = _client(svc)
    r = client.delete("/skills/v1/SKILL000000000000000000000", headers=ALICE)
    assert r.status_code == 204
    assert svc.delete.call_args.kwargs["user_id"] == "alice"


def test_delete_missing_returns_404():
    svc = AsyncMock()
    svc.delete = AsyncMock(side_effect=SkillNotFoundError("nope"))
    client = _client(svc)
    r = client.delete("/skills/v1/x", headers=ALICE)
    assert r.status_code == 404
