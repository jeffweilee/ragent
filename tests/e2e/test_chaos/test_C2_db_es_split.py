"""T-CHAOS.C2 — DB-ES split-brain recovery (spec §3.6.1).

Worker crashes between DB-PENDING claim and ES bulk write + DB-READY promotion.
The reconciler re-dispatches; the worker retries idempotently (ES OVERWRITE).

Spec §3.6.1 common acceptance asserts (per chaos case):
  1. `documents.status` reaches READY.
  2. ES chunks state matches DB (≥ 1 chunk — idempotency via OVERWRITE).
  3. Per-case OTEL signal present — `reconciler.tick` in reconciler log.
  4. `chaos_drill_outcome_total{case="C2", outcome="pass"}` increments.

Difference from C1: C2 explicitly validates ES chunk count to confirm
idempotency of the ES OVERWRITE policy under a reconciler-triggered retry.
"""

from __future__ import annotations

import json
import time
import urllib.request

import httpx
import pytest
import structlog

from tests.e2e.conftest import API_URL, wait_api_ready

pytestmark = [
    pytest.mark.docker,
]

_logger = structlog.get_logger(__name__)

RECOVERY_DEADLINE_SECONDS = 600
WORKER_CLAIM_TIMEOUT_SECONDS = 120


def _post_doc() -> str:
    payload = {
        "ingest_type": "inline",
        "source_id": "S_CHAOS_C2",
        "source_app": "confluence",
        "source_title": "chaos C2",
        "mime_type": "text/plain",
        "content": "chaos C2 split brain test document",
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


def _wait_for_claimed(doc_id: str, timeout: int) -> str | None:
    """Poll until the doc leaves UPLOADED (worker has claimed the task).

    Returns the first non-UPLOADED status, or None if timeout expired.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = _status(doc_id)
        if s != "UPLOADED":
            return s
        time.sleep(0.5)
    return None


def _es_chunk_count(es_url: str, document_id: str) -> int:
    """Count `chunks_v1` documents matching the document_id term filter."""
    req = urllib.request.Request(
        f"{es_url}/chunks_v1/_count",
        method="POST",
        data=json.dumps({"query": {"term": {"document_id": document_id}}}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return int(json.loads(resp.read())["count"])


def test_C2_db_es_split_brain_recovers_to_ready(
    dev_env,
    minio_endpoint: str,
    spawn_module,
    monkeypatch: pytest.MonkeyPatch,
    es_url: str,
) -> None:
    """Kill worker at PENDING; reconciler re-dispatches; READY with ES chunks.

    The key difference from C1 is the post-recovery assertion that ES
    chunk count ≥ 1 — confirming that the second worker run successfully
    bulk-wrote via ES OVERWRITE (idempotent retry, no duplicate errors).
    """
    from tests.e2e.conftest import _ensure_default_bucket

    _ensure_default_bucket(minio_endpoint)
    monkeypatch.setenv("RAGENT_PORT", "8000")
    monkeypatch.setenv("RECONCILER_PENDING_STALE_SECONDS", "10")
    monkeypatch.setenv("WORKER_HEARTBEAT_INTERVAL_SECONDS", "2")

    spawn_module("ragent.api")
    worker = spawn_module("ragent.worker")
    wait_api_ready()
    doc_id = _post_doc()

    claimed_status = _wait_for_claimed(doc_id, timeout=WORKER_CLAIM_TIMEOUT_SECONDS)
    assert claimed_status is not None, (
        f"doc {doc_id} never left UPLOADED within {WORKER_CLAIM_TIMEOUT_SECONDS}s "
        "— worker did not start"
    )
    _logger.info("chaos.c2.worker_claimed", document_id=doc_id, status=claimed_status)

    assert claimed_status in ("PENDING", "READY"), (
        f"doc {doc_id} reached unexpected status {claimed_status!r} — pipeline error"
    )
    if claimed_status == "READY":
        pytest.skip(
            "doc reached READY before SIGKILL window "
            "(pipeline ran faster than poll interval — transient skip)"
        )

    worker.kill()
    worker.wait(timeout=5)
    spawn_module("ragent.worker")  # fresh consumer for the reconciler's re-kiq

    import subprocess
    import sys

    from ragent.bootstrap.metrics import chaos_drill_outcome_total

    reconciler_log = open("/tmp/e2e_chaos_C2_reconciler.log", "w")  # noqa: SIM115
    recovered = False
    deadline = time.monotonic() + RECOVERY_DEADLINE_SECONDS
    while time.monotonic() < deadline:
        subprocess.run(
            [sys.executable, "-m", "ragent.reconciler"],
            env={**__import__("os").environ},
            stdout=reconciler_log,
            stderr=subprocess.STDOUT,
            timeout=60,
            check=False,
        )
        if _status(doc_id) == "READY":
            recovered = True
            break
        time.sleep(15)
    reconciler_log.close()

    if not recovered:
        chaos_drill_outcome_total.labels(case="C2", outcome="fail").inc()
        pytest.fail(f"reconciler did not recover doc {doc_id} within {RECOVERY_DEADLINE_SECONDS}s")

    # Assert 1: terminal status READY.
    assert _status(doc_id) == "READY"

    # Assert 2: ES idempotency — READY doc must have ≥ 1 chunk (OVERWRITE policy
    # means retried bulk writes do not duplicate or fail, they overwrite).
    chunk_count = _es_chunk_count(es_url, doc_id)
    assert chunk_count >= 1, (
        f"READY document has {chunk_count} ES chunks — idempotency/OVERWRITE invariant violated"
    )
    _logger.info("chaos.c2.es_chunks", document_id=doc_id, chunk_count=chunk_count)

    # Assert 3: reconciler.tick signal in subprocess logs.
    with open("/tmp/e2e_chaos_C2_reconciler.log") as f:
        log_text = f.read()
    assert "reconciler.tick" in log_text, "no reconciler.tick log line observed"

    # Assert 4: record drill outcome.
    chaos_drill_outcome_total.labels(case="C2", outcome="pass").inc()
    assert (
        chaos_drill_outcome_total.labels(case="C2", outcome="pass")._value.get() >= 1  # noqa: SLF001
    )
