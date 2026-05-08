"""T2v.45 — Golden end-to-end: v2 ingest across all (ingest_type, mime) cases.

For each combination of ``ingest_type ∈ {inline, file}`` and
``mime_type ∈ {text/plain, text/markdown, text/html}`` (6 cases):
- inline: POST JSON with the content directly.
- file: pre-upload the bytes to MinIO ``__default__``, then POST JSON
  carrying ``{minio_site, object_key}``.

Assertions per case:
- MariaDB ``documents`` row reaches ``READY`` with the right
  ``ingest_type``/``minio_site``/``source_url``/``source_title``.
- ES ``chunks_v1`` has at least one row for the document_id, populated
  with ``content`` (normalized) and ``raw_content`` (original byte
  slice).
- The dropped ``chunks`` DB table is absent (verified once via SHOW
  TABLES).
- Markdown case: fenced code markers survive in ``raw_content``.
- HTML case: ``<script>`` and ``<nav>`` markup is excluded from both
  ``content`` and ``raw_content`` (top-level boilerplate is dropped).

Per-step structured logs are emitted by the worker (T2v.42/43); we
assert here that the document reaches READY, which is a positive
end-to-end signal that the events fired without raising.
"""

from __future__ import annotations

import io
import json
import time
import urllib.request
from typing import Any

import httpx
import pytest
from minio import Minio

from tests.e2e.conftest import API_URL

pytestmark = pytest.mark.docker

DEADLINE_SECONDS = 60
DEFAULT_BUCKET = "ragent-uploads"

_MD_BODY = "# Title\n\nIntro paragraph.\n\n```py\nx = 1\ny = 2\n```\n\nTail.\n"
_HTML_BODY = (
    "<html><body>"
    "<nav>menu drop me</nav>"
    "<script>window.bad=1</script>"
    "<h1>Heading kept</h1>"
    "<p>real paragraph content</p>"
    "</body></html>"
)
_PLAIN_BODY = "First sentence. Second sentence. Third sentence."

CASES: list[dict[str, Any]] = [
    {"ingest_type": "inline", "mime": "text/plain", "body": _PLAIN_BODY},
    {"ingest_type": "inline", "mime": "text/markdown", "body": _MD_BODY},
    {"ingest_type": "inline", "mime": "text/html", "body": _HTML_BODY},
    {"ingest_type": "file", "mime": "text/plain", "body": _PLAIN_BODY},
    {"ingest_type": "file", "mime": "text/markdown", "body": _MD_BODY},
    {"ingest_type": "file", "mime": "text/html", "body": _HTML_BODY},
]


def _ensure_default_bucket(minio_endpoint: str) -> None:
    client = Minio(minio_endpoint, access_key="minioadmin", secret_key="minioadmin", secure=False)
    if not client.bucket_exists(DEFAULT_BUCKET):
        client.make_bucket(DEFAULT_BUCKET)


def _put_object(minio_endpoint: str, object_key: str, body: bytes, content_type: str) -> None:
    client = Minio(minio_endpoint, access_key="minioadmin", secret_key="minioadmin", secure=False)
    client.put_object(
        DEFAULT_BUCKET,
        object_key,
        io.BytesIO(body),
        length=len(body),
        content_type=content_type,
    )


def _post_inline(case: dict[str, Any], idx: int) -> str:
    payload = {
        "ingest_type": "inline",
        "source_id": f"GOLDEN-INLINE-{idx}",
        "source_app": "golden",
        "source_title": f"golden inline {idx}",
        "source_url": f"https://example.test/inline/{idx}",
        "mime_type": case["mime"],
        "content": case["body"],
    }
    resp = httpx.post(
        f"{API_URL}/ingest",
        headers={"X-User-Id": "alice"},
        json=payload,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["document_id"]


def _post_file(case: dict[str, Any], idx: int, minio_endpoint: str) -> str:
    object_key = f"golden_file_{idx}_{int(time.time() * 1000)}"
    _put_object(minio_endpoint, object_key, case["body"].encode(), case["mime"])
    payload = {
        "ingest_type": "file",
        "source_id": f"GOLDEN-FILE-{idx}",
        "source_app": "golden",
        "source_title": f"golden file {idx}",
        "source_url": f"https://example.test/file/{idx}",
        "mime_type": case["mime"],
        "minio_site": "__default__",
        "object_key": object_key,
    }
    resp = httpx.post(
        f"{API_URL}/ingest",
        headers={"X-User-Id": "alice"},
        json=payload,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["document_id"]


def _poll_until_ready(doc_id: str) -> str:
    deadline = time.monotonic() + DEADLINE_SECONDS
    last = "UNKNOWN"
    while time.monotonic() < deadline:
        last = (
            httpx.get(f"{API_URL}/ingest/{doc_id}", headers={"X-User-Id": "alice"}, timeout=5)
            .json()
            .get("status", "UNKNOWN")
        )
        if last in ("READY", "FAILED"):
            return last
        time.sleep(1)
    return last


def _es_search(es_url: str, document_id: str) -> list[dict]:
    body = json.dumps({"query": {"term": {"document_id": document_id}}, "size": 50}).encode()
    req = urllib.request.Request(
        f"{es_url}/chunks_v1/_search",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read())
    return [hit["_source"] for hit in result.get("hits", {}).get("hits", [])]


def _es_refresh(es_url: str) -> None:
    with urllib.request.urlopen(f"{es_url}/chunks_v1/_refresh", timeout=10):
        pass


def _doc_row(doc_id: str) -> dict:
    return httpx.get(f"{API_URL}/ingest/{doc_id}", headers={"X-User-Id": "alice"}, timeout=5).json()


def _verify_chunks_table_dropped(mariadb_dsn: str) -> None:
    """SHOW TABLES must not list `chunks` (003_drop_chunks.sql)."""
    from sqlalchemy import create_engine, text

    sync_dsn = mariadb_dsn.replace("mysql+aiomysql://", "mysql+pymysql://")
    engine = create_engine(sync_dsn)
    with engine.connect() as conn:
        rows = conn.execute(text("SHOW TABLES")).fetchall()
    table_names = {row[0] for row in rows}
    assert "chunks" not in table_names, f"`chunks` table should be dropped; saw {table_names}"


def test_v2_golden_end_to_end(
    running_stack, e2e_env, es_url: str, minio_endpoint: str, mariadb_dsn: str
) -> None:
    _ensure_default_bucket(minio_endpoint)

    _verify_chunks_table_dropped(mariadb_dsn)

    results: list[dict[str, Any]] = []
    for idx, case in enumerate(CASES):
        if case["ingest_type"] == "inline":
            doc_id = _post_inline(case, idx)
        else:
            doc_id = _post_file(case, idx, minio_endpoint)
        status = _poll_until_ready(doc_id)
        assert status == "READY", (
            f"case {case} (doc_id={doc_id}) did not reach READY: status={status}"
        )
        results.append({"doc_id": doc_id, "case": case})

    _es_refresh(es_url)

    for r in results:
        doc_id = r["doc_id"]
        case = r["case"]
        # Assert MariaDB row state
        row = _doc_row(doc_id)
        assert row["status"] == "READY"
        assert row["ingest_type"] == case["ingest_type"]
        assert row["source_title"].startswith("golden ")
        if case["ingest_type"] == "file":
            assert row["minio_site"] == "__default__"
        else:
            assert row.get("minio_site") in (None, "")

        # Assert ES chunks
        hits = _es_search(es_url, doc_id)
        assert len(hits) >= 1, f"no ES rows for {doc_id} ({case})"
        for src in hits:
            content = src.get("content") or src.get("text") or ""
            raw = src.get("raw_content") or ""
            assert content, f"empty content for {doc_id}"
            assert raw, f"empty raw_content for {doc_id} ({case})"
            if case["mime"] == "text/html":
                assert "<script>" not in content
                assert "<script>" not in raw
                assert "<nav>" not in content
                assert "<nav>" not in raw

        if case["mime"] == "text/markdown":
            joined_raw = "\n".join(h.get("raw_content") or "" for h in hits)
            assert "```" in joined_raw, f"markdown fence dropped for {doc_id}"
