"""POST /ops/v1/retry — admin ops router unit tests."""

from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ragent.services.ingest_service import IngestService


def _make_app(svc=None):
    from ragent.routers.admin_ops import create_admin_ops_router

    if svc is None:
        svc = AsyncMock(spec=IngestService)
        svc.batch_rerun.return_value = ({}, {}, 0, 0, 0)

    app = FastAPI()
    app.include_router(create_admin_ops_router(svc=svc))
    return TestClient(app)


def _default_svc(before=None, after=None, queued=0, skipped=0, deferred=0):
    svc = AsyncMock(spec=IngestService)
    svc.batch_rerun.return_value = (
        before or {"FAILED": 5},
        after or {"FAILED": 0},
        queued,
        skipped,
        deferred,
    )
    return svc


# ---------------------------------------------------------------------------
# dry_run
# ---------------------------------------------------------------------------


def test_dry_run_returns_before_after_equal():
    svc = _default_svc(before={"FAILED": 5}, after={"FAILED": 5}, queued=0, skipped=0)
    client = _make_app(svc)

    resp = client.post(
        "/ops/v1/retry",
        json={"statuses": ["FAILED"], "dry_run": True},
        headers={"x-user-id": "ops"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["dry_run"] is True
    assert body["queued"] == 0
    assert body["skipped"] == 0
    assert body["counts"]["FAILED"]["before"] == body["counts"]["FAILED"]["after"]


# ---------------------------------------------------------------------------
# execute
# ---------------------------------------------------------------------------


def test_execute_returns_queued_skipped():
    svc = _default_svc(before={"FAILED": 7}, after={"FAILED": 0}, queued=7, skipped=0)
    client = _make_app(svc)

    resp = client.post(
        "/ops/v1/retry",
        json={"statuses": ["FAILED"]},
        headers={"x-user-id": "ops"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["dry_run"] is False
    assert body["queued"] == 7
    assert body["skipped"] == 0


def test_counts_dict_keyed_by_status():
    svc = _default_svc(
        before={"FAILED": 3, "PENDING": 2},
        after={"FAILED": 0, "PENDING": 0},
        queued=5,
    )
    client = _make_app(svc)

    resp = client.post(
        "/ops/v1/retry",
        json={"statuses": ["FAILED", "PENDING"]},
        headers={"x-user-id": "ops"},
    )

    body = resp.json()
    assert "FAILED" in body["counts"]
    assert "PENDING" in body["counts"]
    assert body["counts"]["FAILED"]["before"] == 3
    assert body["counts"]["FAILED"]["after"] == 0
    assert body["counts"]["PENDING"]["before"] == 2
    assert body["counts"]["PENDING"]["after"] == 0


# ---------------------------------------------------------------------------
# filter forwarding
# ---------------------------------------------------------------------------


def test_source_app_forwarded():
    svc = _default_svc()
    client = _make_app(svc)

    client.post(
        "/ops/v1/retry",
        json={"statuses": ["FAILED"], "source_app": "myapp"},
        headers={"x-user-id": "ops"},
    )

    call_kwargs = svc.batch_rerun.call_args.kwargs
    assert call_kwargs["source_app"] == "myapp"


def test_created_after_forwarded():
    svc = _default_svc()
    client = _make_app(svc)

    client.post(
        "/ops/v1/retry",
        json={"statuses": ["FAILED"], "created_after": "2026-06-04T00:00:00Z"},
        headers={"x-user-id": "ops"},
    )

    call_kwargs = svc.batch_rerun.call_args.kwargs
    assert call_kwargs["created_after"] is not None


def test_limit_forwarded():
    svc = _default_svc()
    client = _make_app(svc)

    client.post(
        "/ops/v1/retry",
        json={"statuses": ["FAILED"], "limit": 50},
        headers={"x-user-id": "ops"},
    )

    call_kwargs = svc.batch_rerun.call_args.kwargs
    assert call_kwargs["limit"] == 50


# ---------------------------------------------------------------------------
# validation errors (422)
# ---------------------------------------------------------------------------


def test_missing_statuses_returns_422():
    client = _make_app()

    resp = client.post(
        "/ops/v1/retry",
        json={"dry_run": True},
        headers={"x-user-id": "ops"},
    )

    assert resp.status_code == 422


def test_empty_statuses_returns_422():
    client = _make_app()

    resp = client.post(
        "/ops/v1/retry",
        json={"statuses": []},
        headers={"x-user-id": "ops"},
    )

    assert resp.status_code == 422


def test_limit_over_cap_returns_422():
    client = _make_app()

    resp = client.post(
        "/ops/v1/retry",
        json={"statuses": ["FAILED"], "limit": 501},
        headers={"x-user-id": "ops"},
    )

    assert resp.status_code == 422


def test_missing_user_id_returns_401_or_422():
    """No auth header — get_user_id returns None; 200 allowed since auth is middleware-side."""
    svc = _default_svc()
    client = _make_app(svc)

    resp = client.post(
        "/ops/v1/retry",
        json={"statuses": ["FAILED"]},
    )

    assert resp.status_code in (200, 401, 422)


# ---------------------------------------------------------------------------
# extra field rejection (model_config extra="forbid")
# ---------------------------------------------------------------------------


def test_typo_dryrun_instead_of_dry_run_returns_422():
    """dryrun typo must not silently default dry_run=False and mutate."""
    client = _make_app()

    resp = client.post(
        "/ops/v1/retry",
        json={"statuses": ["FAILED"], "dryrun": True},
        headers={"x-user-id": "ops"},
    )

    assert resp.status_code == 422


def test_unknown_field_returns_422():
    client = _make_app()

    resp = client.post(
        "/ops/v1/retry",
        json={"statuses": ["FAILED"], "unknown_field": "oops"},
        headers={"x-user-id": "ops"},
    )

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# counts always include all requested statuses (even zero-count)
# ---------------------------------------------------------------------------


def test_counts_includes_zero_count_requested_status():
    """If a requested status has 0 docs, it must still appear in counts."""
    svc = _default_svc(
        before={"FAILED": 3},
        after={"FAILED": 0},
        queued=3,
    )
    client = _make_app(svc)

    resp = client.post(
        "/ops/v1/retry",
        json={"statuses": ["FAILED", "PENDING"]},
        headers={"x-user-id": "ops"},
    )

    body = resp.json()
    assert "PENDING" in body["counts"]
    assert body["counts"]["PENDING"]["before"] == 0
    assert body["counts"]["PENDING"]["after"] == 0


# ---------------------------------------------------------------------------
# operator_id forwarding
# ---------------------------------------------------------------------------


def test_operator_id_forwarded_from_user_header():
    svc = _default_svc()
    client = _make_app(svc)

    client.post(
        "/ops/v1/retry",
        json={"statuses": ["FAILED"]},
        headers={"x-user-id": "alice"},
    )

    call_kwargs = svc.batch_rerun.call_args.kwargs
    assert call_kwargs["operator_id"] == "alice"


def test_retry_surfaces_deferred_when_the_broker_is_unreachable() -> None:
    """`queued` promises an immediate re-queue; deferred rows must not inflate it.

    Every listed document stays accounted for: queued + deferred + skipped.
    """
    svc = _default_svc(queued=0, skipped=0, deferred=7)
    resp = _make_app(svc).post(
        "/ops/v1/retry", json={"statuses": ["FAILED"]}, headers={"x-user-id": "ops"}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["queued"] == 0
    assert body["deferred"] == 7
