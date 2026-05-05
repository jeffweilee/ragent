"""T7.4 — Chaos: kill worker mid-ingest → Reconciler recovers ≤ 10 min."""

from __future__ import annotations

import io
import os
import signal
import subprocess
import sys
import time

import httpx
import pytest

pytestmark = pytest.mark.docker

API_URL = "http://127.0.0.1:8000"
RECOVERY_DEADLINE_SECONDS = 600


def _set_env(monkeypatch, mariadb_dsn: str, es_url: str, minio_endpoint: str) -> None:
    monkeypatch.setenv("RAGENT_ENV", "dev")
    monkeypatch.setenv("RAGENT_AUTH_DISABLED", "true")
    monkeypatch.setenv("RAGENT_HOST", "127.0.0.1")
    monkeypatch.setenv("MARIADB_DSN", mariadb_dsn)
    monkeypatch.setenv("ES_HOSTS", es_url)
    monkeypatch.setenv("ES_VERIFY_CERTS", "false")
    monkeypatch.setenv("MINIO_ENDPOINT", minio_endpoint)
    monkeypatch.setenv("MINIO_ACCESS_KEY", "minioadmin")
    monkeypatch.setenv("MINIO_SECRET_KEY", "minioadmin")
    # Tighten thresholds for the test
    monkeypatch.setenv("RECONCILER_PENDING_STALE_SECONDS", "10")
    monkeypatch.setenv("WORKER_HEARTBEAT_INTERVAL_SECONDS", "2")


def _spawn(module: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", module],
        env={**os.environ},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _wait_api_ready(timeout: int = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{API_URL}/livez", timeout=2).status_code == 200:
                return
        except Exception:
            time.sleep(0.5)
    raise TimeoutError("API never reached /livez=200")


def _post_doc() -> str:
    files = {"file": ("doc.txt", io.BytesIO(b"chaos test document"), "text/plain")}
    data = {"source_id": "S_CHAOS", "source_app": "confluence", "source_title": "chaos"}
    resp = httpx.post(
        f"{API_URL}/ingest", headers={"X-User-Id": "alice"}, data=data, files=files, timeout=10
    )
    resp.raise_for_status()
    return resp.json()["document_id"]


def test_worker_kill_mid_ingest_recovers(
    monkeypatch: pytest.MonkeyPatch, mariadb_dsn: str, es_url: str, minio_endpoint: str
) -> None:
    """Kill worker after PENDING transition → reconciler re-dispatches → READY before deadline."""
    _set_env(monkeypatch, mariadb_dsn, es_url, minio_endpoint)
    api = _spawn("ragent.api")
    worker = _spawn("ragent.worker")
    try:
        _wait_api_ready()
        doc_id = _post_doc()

        # Wait for status to become PENDING (worker picked it up), then kill worker
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            status = (
                httpx.get(f"{API_URL}/ingest/{doc_id}", headers={"X-User-Id": "alice"}, timeout=5)
                .json()
                .get("status")
            )
            if status == "PENDING":
                break
            time.sleep(0.5)
        worker.send_signal(signal.SIGKILL)
        worker.wait(timeout=5)

        # No worker is running. Re-spawn a fresh worker so the reconciler's re-kiq
        # has a consumer. (Reconciler's re-dispatch alone doesn't process the doc.)
        worker2 = _spawn("ragent.worker")
        try:
            recovery_deadline = time.monotonic() + RECOVERY_DEADLINE_SECONDS
            while time.monotonic() < recovery_deadline:
                # Trigger reconciler tick
                subprocess.run(
                    [sys.executable, "-m", "ragent.reconciler"],
                    env={**os.environ},
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=30,
                )
                status = (
                    httpx.get(
                        f"{API_URL}/ingest/{doc_id}", headers={"X-User-Id": "alice"}, timeout=5
                    )
                    .json()
                    .get("status")
                )
                if status == "READY":
                    return
                time.sleep(15)
            pytest.fail(
                f"Reconciler did not recover doc {doc_id} within {RECOVERY_DEADLINE_SECONDS}s"
            )
        finally:
            worker2.terminate()
            worker2.wait(timeout=5)
    finally:
        api.terminate()
        api.wait(timeout=5)
