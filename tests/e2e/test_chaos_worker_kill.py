"""T7.4 — Chaos: kill worker mid-ingest → Reconciler recovers ≤ 10 min."""

from __future__ import annotations

import signal
import time

import httpx
import pytest

from tests.e2e.conftest import API_URL, wait_api_ready

pytestmark = [
    pytest.mark.docker,
    # Deferred until the chaos suite track expands this single happy-path
    # kill into the full C1–C7 partial-failure matrix (see SRE journal +
    # docs/00_plan.md T7.4.x). Kept as scaffolding for that work; not run
    # in any current gate. xfail(run=False) is preferred over pytest.skip()
    # so the marker is structurally a "known deferred", not a silent TODO.
    pytest.mark.xfail(
        run=False,
        reason="Deferred to chaos suite track T7.4.x; current single-case "
        "test is scaffolding only. Engine-per-tick refactor has landed "
        "(T7.4.x(a)); remaining blocker is the fault-injection matrix "
        "(T7.4.x(b)).",
    ),
]

RECOVERY_DEADLINE_SECONDS = 600


def _post_doc() -> str:
    payload = {
        "ingest_type": "inline",
        "source_id": "S_CHAOS",
        "source_app": "confluence",
        "source_title": "chaos",
        "mime_type": "text/plain",
        "content": "chaos test document",
    }
    resp = httpx.post(
        f"{API_URL}/ingest/v1",
        headers={"X-User-Id": "alice"},
        json=payload,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["document_id"]


def _status(doc_id: str) -> str:
    return (
        httpx.get(f"{API_URL}/ingest/v1/{doc_id}", headers={"X-User-Id": "alice"}, timeout=5)
        .json()
        .get("status")
    )


def _wait_for_status(doc_id: str, target: str, timeout: int) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _status(doc_id) == target:
            return True
        time.sleep(0.5)
    return False


def test_worker_kill_mid_ingest_recovers(
    e2e_env, spawn_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kill worker after PENDING transition → reconciler re-dispatches → READY before deadline."""
    monkeypatch.setenv("RECONCILER_PENDING_STALE_SECONDS", "10")
    monkeypatch.setenv("WORKER_HEARTBEAT_INTERVAL_SECONDS", "2")

    spawn_module("ragent.api")
    worker = spawn_module("ragent.worker")
    wait_api_ready()
    doc_id = _post_doc()
    _wait_for_status(doc_id, "PENDING", timeout=30)

    worker.send_signal(signal.SIGKILL)
    worker.wait(timeout=5)
    spawn_module("ragent.worker")  # fresh consumer for the reconciler's re-kiq

    from ragent.reconciler import _build_from_env

    reconciler = _build_from_env()
    deadline = time.monotonic() + RECOVERY_DEADLINE_SECONDS
    while time.monotonic() < deadline:
        reconciler.run()
        if _status(doc_id) == "READY":
            return
        time.sleep(15)
    pytest.fail(f"Reconciler did not recover doc {doc_id} within {RECOVERY_DEADLINE_SECONDS}s")
