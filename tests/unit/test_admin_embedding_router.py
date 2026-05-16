"""T-EM.12 — admin/embedding router (B50 §5).

Five lifecycle endpoints + GET /state + GET /cutover/preflight, all under
`/embedding/v1`. Each endpoint:

- 200 with snapshot dict on success.
- 409 problem+json with `EMBEDDING_LIFECYCLE_INVALID_STATE` on FSM rejection.
- 409 problem+json with `EMBEDDING_CUTOVER_PREFLIGHT_FAILED` on hard-gate fail.
- 422 problem+json with `EMBEDDING_INVALID_CONFIG` on bad promote payload.
- 422 problem+json with `EMBEDDING_FIELD_NAME_COLLISION` when promote target
  field already in mapping.

Router is a thin parse → delegate → problem layer per CLAUDE.md.
"""

from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_app(svc, snapshot=None):
    from ragent.routers.admin_embedding import create_router

    app = FastAPI()
    app.include_router(
        create_router(service=svc, snapshot_provider=snapshot or (lambda: {"state": "IDLE"}))
    )
    return TestClient(app)


# ---------------------------------------------------------------------------
# promote
# ---------------------------------------------------------------------------


def test_promote_happy_path_returns_200() -> None:
    svc = AsyncMock()
    svc.promote.return_value = {"state": "CANDIDATE", "candidate": {"name": "x"}}

    client = _make_app(svc)
    resp = client.post(
        "/embedding/v1/promote",
        json={"name": "bge-m3-v2", "dim": 768, "api_url": "http://e", "model_arg": "bge-m3-v2"},
    )

    assert resp.status_code == 200
    assert resp.json()["state"] == "CANDIDATE"
    svc.promote.assert_awaited_once_with(
        name="bge-m3-v2", dim=768, api_url="http://e", model_arg="bge-m3-v2"
    )


def test_promote_invalid_state_returns_409() -> None:
    from ragent.utility.embedding_lifecycle import IllegalEmbeddingTransition

    svc = AsyncMock()
    svc.promote.side_effect = IllegalEmbeddingTransition("nope")
    client = _make_app(svc)

    resp = client.post(
        "/embedding/v1/promote",
        json={"name": "x", "dim": 768, "api_url": "u", "model_arg": "x"},
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["error_code"] == "EMBEDDING_LIFECYCLE_INVALID_STATE"


def test_promote_bad_dim_returns_422() -> None:
    from ragent.clients.embedding_model_config import InvalidEmbeddingModelConfig

    svc = AsyncMock()
    svc.promote.side_effect = InvalidEmbeddingModelConfig("dim out of range")
    client = _make_app(svc)

    resp = client.post(
        "/embedding/v1/promote",
        json={"name": "x", "dim": 10_000, "api_url": "u", "model_arg": "x"},
    )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "EMBEDDING_INVALID_CONFIG"


def test_promote_field_collision_returns_422() -> None:
    from ragent.services.embedding_lifecycle_service import EmbeddingFieldCollision

    svc = AsyncMock()
    svc.promote.side_effect = EmbeddingFieldCollision("already mapped")
    client = _make_app(svc)

    resp = client.post(
        "/embedding/v1/promote",
        json={"name": "x", "dim": 768, "api_url": "u", "model_arg": "x"},
    )
    assert resp.status_code == 422
    assert resp.json()["error_code"] == "EMBEDDING_FIELD_NAME_COLLISION"


# ---------------------------------------------------------------------------
# cutover
# ---------------------------------------------------------------------------


def test_cutover_happy_path() -> None:
    svc = AsyncMock()
    svc.cutover.return_value = {"state": "CUTOVER"}
    client = _make_app(svc)
    resp = client.post("/embedding/v1/cutover", json={})
    assert resp.status_code == 200
    assert resp.json()["state"] == "CUTOVER"
    svc.cutover.assert_awaited_once_with(force=False)


def test_cutover_with_force_true() -> None:
    svc = AsyncMock()
    svc.cutover.return_value = {"state": "CUTOVER"}
    client = _make_app(svc)
    client.post("/embedding/v1/cutover", json={"force": True})
    svc.cutover.assert_awaited_once_with(force=True)


def test_cutover_preflight_fail_returns_409() -> None:
    from ragent.services.embedding_lifecycle_service import CutoverPreflightFailed

    svc = AsyncMock()
    svc.cutover.side_effect = CutoverPreflightFailed({"pass": False, "gates": []})
    client = _make_app(svc)

    resp = client.post("/embedding/v1/cutover", json={})
    assert resp.status_code == 409
    body = resp.json()
    assert body["error_code"] == "EMBEDDING_CUTOVER_PREFLIGHT_FAILED"
    assert "preflight" in body
    assert body["preflight"]["pass"] is False


def test_cutover_invalid_state_returns_409() -> None:
    from ragent.utility.embedding_lifecycle import IllegalEmbeddingTransition

    svc = AsyncMock()
    svc.cutover.side_effect = IllegalEmbeddingTransition("not candidate")
    client = _make_app(svc)
    resp = client.post("/embedding/v1/cutover", json={})
    assert resp.status_code == 409
    assert resp.json()["error_code"] == "EMBEDDING_LIFECYCLE_INVALID_STATE"


# ---------------------------------------------------------------------------
# rollback / commit / abort
# ---------------------------------------------------------------------------


def test_rollback_invokes_service() -> None:
    svc = AsyncMock()
    svc.rollback.return_value = {"state": "CANDIDATE"}
    client = _make_app(svc)
    resp = client.post("/embedding/v1/rollback")
    assert resp.status_code == 200
    svc.rollback.assert_awaited_once()


def test_commit_invokes_service() -> None:
    svc = AsyncMock()
    svc.commit.return_value = {"state": "IDLE"}
    client = _make_app(svc)
    resp = client.post("/embedding/v1/commit")
    assert resp.status_code == 200
    svc.commit.assert_awaited_once()


def test_abort_invokes_service() -> None:
    svc = AsyncMock()
    svc.abort.return_value = {"state": "IDLE"}
    client = _make_app(svc)
    resp = client.post("/embedding/v1/abort")
    assert resp.status_code == 200
    svc.abort.assert_awaited_once()


# ---------------------------------------------------------------------------
# state / preflight
# ---------------------------------------------------------------------------


def test_state_returns_503_when_registry_not_ready() -> None:
    from ragent.services.active_model_registry import ActiveModelRegistryNotReady

    def _snapshot() -> dict:
        raise ActiveModelRegistryNotReady("first refresh has not succeeded")

    client = _make_app(AsyncMock(), snapshot=_snapshot)
    resp = client.get("/embedding/v1/state")
    assert resp.status_code == 503
    assert resp.json()["error_code"] == "EMBEDDING_REGISTRY_NOT_READY"


def test_state_returns_snapshot() -> None:
    def _snapshot() -> dict:
        return {
            "state": "CUTOVER",
            "stable": {"name": "bge-m3"},
            "candidate": {"name": "bge-m3-v2"},
            "read": "candidate",
            "retired": [],
        }

    client = _make_app(AsyncMock(), snapshot=_snapshot)
    resp = client.get("/embedding/v1/state")
    assert resp.status_code == 200
    assert resp.json()["state"] == "CUTOVER"


def test_preflight_endpoint_reports_pass() -> None:
    svc = AsyncMock()
    svc.preflight.return_value = {"pass": True, "gates": []}
    client = _make_app(svc)
    resp = client.get("/embedding/v1/cutover/preflight")
    assert resp.status_code == 200
    assert resp.json()["pass"] is True
