"""T7.2 — Quickstart E2E: 100 docs → ≥99% READY in 60s via real API+worker subprocesses (B30)."""

from __future__ import annotations

import io
import os
import subprocess
import sys
import time

import httpx
import pytest

pytestmark = pytest.mark.docker

API_URL = "http://127.0.0.1:8000"
TARGET_COUNT = 100
SUCCESS_THRESHOLD = 0.99
DEADLINE_SECONDS = 60


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


def _post_doc(idx: int) -> str:
    headers = {"X-User-Id": "alice"}
    files = {
        "file": (f"doc_{idx}.txt", io.BytesIO(f"document {idx} content".encode()), "text/plain")
    }
    data = {
        "source_id": f"S{idx}",
        "source_app": "confluence",
        "source_title": f"doc {idx}",
    }
    resp = httpx.post(f"{API_URL}/ingest", headers=headers, data=data, files=files, timeout=10)
    resp.raise_for_status()
    return resp.json()["document_id"]


def _poll_status(doc_id: str) -> str:
    resp = httpx.get(f"{API_URL}/ingest/{doc_id}", headers={"X-User-Id": "alice"}, timeout=5)
    return resp.json().get("status", "UNKNOWN")


def test_quickstart_99pct_ready_in_60s(
    monkeypatch: pytest.MonkeyPatch, mariadb_dsn: str, es_url: str, minio_endpoint: str
) -> None:
    _set_env(monkeypatch, mariadb_dsn, es_url, minio_endpoint)
    api_proc = _spawn("ragent.api")
    worker_proc = _spawn("ragent.worker")
    try:
        _wait_api_ready()
        doc_ids = [_post_doc(i) for i in range(TARGET_COUNT)]
        deadline = time.monotonic() + DEADLINE_SECONDS
        while time.monotonic() < deadline:
            ready = sum(1 for d in doc_ids if _poll_status(d) == "READY")
            if ready / TARGET_COUNT >= SUCCESS_THRESHOLD:
                return
            time.sleep(2)
        ready = sum(1 for d in doc_ids if _poll_status(d) == "READY")
        pytest.fail(f"Only {ready}/{TARGET_COUNT} reached READY within {DEADLINE_SECONDS}s")
    finally:
        api_proc.terminate()
        worker_proc.terminate()
        api_proc.wait(timeout=5)
        worker_proc.wait(timeout=5)
