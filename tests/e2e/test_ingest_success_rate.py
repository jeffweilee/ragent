"""T7.2 — Quickstart E2E: 100 docs → ≥99% READY in 60s via real API+worker subprocesses (B30)."""

from __future__ import annotations

import time

import httpx
import pytest

from tests.e2e.conftest import API_URL, wait_api_ready

pytestmark = pytest.mark.docker

TARGET_COUNT = 100
SUCCESS_THRESHOLD = 0.99
DEADLINE_SECONDS = 60


def _post_doc(idx: int) -> str:
    payload = {
        "ingest_type": "inline",
        "source_id": f"S{idx}",
        "source_app": "confluence",
        "source_title": f"doc {idx}",
        "content_type": "text/plain",
        "content": f"document {idx} content",
    }
    resp = httpx.post(
        f"{API_URL}/ingest",
        headers={"X-User-Id": "alice"},
        json=payload,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["document_id"]


def _poll_status(doc_id: str) -> str:
    return (
        httpx.get(f"{API_URL}/ingest/{doc_id}", headers={"X-User-Id": "alice"}, timeout=5)
        .json()
        .get("status", "UNKNOWN")
    )


def test_quickstart_99pct_ready_in_60s(e2e_env, spawn_module) -> None:
    spawn_module("ragent.api")
    spawn_module("ragent.worker")
    wait_api_ready()

    doc_ids = [_post_doc(i) for i in range(TARGET_COUNT)]
    deadline = time.monotonic() + DEADLINE_SECONDS
    while time.monotonic() < deadline:
        ready = sum(1 for d in doc_ids if _poll_status(d) == "READY")
        if ready / TARGET_COUNT >= SUCCESS_THRESHOLD:
            return
        time.sleep(2)
    ready = sum(1 for d in doc_ids if _poll_status(d) == "READY")
    pytest.fail(f"Only {ready}/{TARGET_COUNT} reached READY within {DEADLINE_SECONDS}s")
