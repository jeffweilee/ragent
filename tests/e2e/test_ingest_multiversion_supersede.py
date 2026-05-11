"""E2E: 5 articles × 3 versions — supersede leaves one READY doc per source_id.

15 ingest requests (5 distinct source_ids × 3 content revisions) are posted
in a random order against a live API + worker.  The atomic
``promote_to_ready_and_demote_siblings`` DB election ensures the row with the
highest ``created_at`` survives as READY; all earlier revisions reach DELETING.

Assertions:
- Every one of the 5 expected winners reaches READY within the deadline.
- Every one of the 10 expected losers reaches DELETING (or is already gone).
- Each winner's ES ``chunks_v1`` rows contain text unique to its version, i.e.
  the content of the last-submitted revision is what ended up indexed.
"""

from __future__ import annotations

import json
import random
import time
import urllib.request
from typing import Any

import httpx
import pytest

from tests.e2e.conftest import API_URL

pytestmark = pytest.mark.docker

DEADLINE_SECONDS = 120
SOURCE_APP = "e2e-multiversion"

# ── article corpus ──────────────────────────────────────────────────────────
# 5 articles, 3 progressively-revised versions each.  Version text contains a
# unique marker ("v1 / v2 / v3") so we can confirm the right revision landed.

_ARTICLES: list[dict[str, Any]] = [
    {
        "source_id": f"article-mv-{i}",
        "versions": [
            (
                f"Article {i} — v1: The standard Lorem Ipsum passage used since "
                f"the 1500s when an unknown printer scrambled a galley of type."
            ),
            (
                f"Article {i} — v2: Revised. Contrary to popular belief, Lorem "
                f"Ipsum is not simply random text; it has roots in classical Latin."
            ),
            (
                f"Article {i} — v3: Final edit. Richard McClintock, a Latin "
                f"professor, discovered the undoubtable source of Lorem Ipsum."
            ),
        ],
    }
    for i in range(1, 6)
]

# unique substring present in each version — used for ES content assertions
_VERSION_MARKER = ["v1:", "v2:", "v3:"]


# ── helpers ──────────────────────────────────────────────────────────────────


def _post_inline(source_id: str, version: int, body: str) -> str:
    """POST one inline ingest; return the new document_id."""
    payload = {
        "ingest_type": "inline",
        "source_id": source_id,
        "source_app": SOURCE_APP,
        "source_title": f"{source_id} v{version}",
        "mime_type": "text/plain",
        "content": body,
    }
    resp = httpx.post(
        f"{API_URL}/ingest",
        headers={"X-User-Id": "alice"},
        json=payload,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["document_id"]


def _get_status(doc_id: str) -> str:
    """Return status string or 'DELETED' on 404."""
    resp = httpx.get(
        f"{API_URL}/ingest/{doc_id}",
        headers={"X-User-Id": "alice"},
        timeout=5,
    )
    if resp.status_code == 404:
        return "DELETED"
    return resp.json().get("status", "UNKNOWN")


def _poll_all_until_terminal(doc_ids: list[str]) -> dict[str, str]:
    """Return {doc_id: final_status} once every doc reaches a terminal state.

    Terminal states: READY, FAILED, DELETING, DELETED (404).
    Documents still UPLOADED or PENDING after the deadline are recorded as
    TIMEOUT.
    """
    deadline = time.monotonic() + DEADLINE_SECONDS
    statuses: dict[str, str] = {d: "UNKNOWN" for d in doc_ids}
    pending = set(doc_ids)

    while pending and time.monotonic() < deadline:
        still_pending: set[str] = set()
        for doc_id in pending:
            s = _get_status(doc_id)
            if s in ("READY", "FAILED", "DELETING", "DELETED"):
                statuses[doc_id] = s
            else:
                still_pending.add(doc_id)
        pending = still_pending
        if pending:
            time.sleep(1)

    for doc_id in pending:
        statuses[doc_id] = "TIMEOUT"

    return statuses


def _es_refresh(es_url: str) -> None:
    req = urllib.request.Request(
        f"{es_url}/chunks_v1/_refresh",
        method="POST",
    )
    urllib.request.urlopen(req, timeout=10).close()


def _es_chunks(es_url: str, document_id: str) -> list[dict]:
    body = json.dumps({"query": {"term": {"document_id": document_id}}, "size": 50}).encode()
    req = urllib.request.Request(
        f"{es_url}/chunks_v1/_search",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read())
    return [hit["_source"] for hit in result.get("hits", {}).get("hits", [])]


# ── test ─────────────────────────────────────────────────────────────────────


def test_multiversion_supersede_five_ready_latest_content(
    running_stack, e2e_env, es_url: str
) -> None:
    """5 articles × 3 versions posted in random order → 5 READY, latest content."""

    # Build 15 (source_id, version_number, body) tuples and shuffle them.
    # A fixed seed makes CI failures reproducible while still exercising
    # out-of-order arrival.
    requests: list[dict[str, Any]] = []
    for article in _ARTICLES:
        for v_idx, body in enumerate(article["versions"]):
            requests.append(
                {
                    "source_id": article["source_id"],
                    "version": v_idx + 1,
                    "body": body,
                }
            )
    random.seed(42)
    random.shuffle(requests)

    # Submit all 15.  Track the LAST submission per source_id — that doc_id
    # has the highest created_at and is therefore the DB-elected survivor.
    all_entries: list[dict[str, Any]] = []
    last_per_source: dict[str, dict[str, Any]] = {}

    for req in requests:
        doc_id = _post_inline(req["source_id"], req["version"], req["body"])
        entry = {
            "doc_id": doc_id,
            "source_id": req["source_id"],
            "version": req["version"],
            "body": req["body"],
        }
        all_entries.append(entry)
        last_per_source[req["source_id"]] = entry

    assert len(all_entries) == 15
    assert len(last_per_source) == 5

    winner_doc_ids = {e["doc_id"] for e in last_per_source.values()}
    loser_entries = [e for e in all_entries if e["doc_id"] not in winner_doc_ids]
    assert len(loser_entries) == 10

    # Wait for all 15 pipelines to finish.
    all_doc_ids = [e["doc_id"] for e in all_entries]
    statuses = _poll_all_until_terminal(all_doc_ids)

    timed_out = [d for d, s in statuses.items() if s == "TIMEOUT"]
    assert not timed_out, f"documents did not reach terminal state: {timed_out}"

    failed = [d for d, s in statuses.items() if s == "FAILED"]
    assert not failed, f"pipeline failures: {failed}"

    # ── winner assertions ────────────────────────────────────────────────────
    non_ready_winners = [e for e in last_per_source.values() if statuses[e["doc_id"]] != "READY"]
    assert not non_ready_winners, "expected winners to be READY; got: " + str(
        [(e["source_id"], e["version"], statuses[e["doc_id"]]) for e in non_ready_winners]
    )

    # ── loser assertions ─────────────────────────────────────────────────────
    # Losers are atomically demoted to DELETING by the DB election (no
    # reconciler runs during e2e, so rows stay at DELETING rather than
    # disappearing).
    unexpected_loser_states = [
        e for e in loser_entries if statuses[e["doc_id"]] not in ("DELETING", "DELETED")
    ]
    assert not unexpected_loser_states, "expected losers to be DELETING/DELETED; got: " + str(
        [(e["source_id"], e["version"], statuses[e["doc_id"]]) for e in unexpected_loser_states]
    )

    # ── ES content assertions ────────────────────────────────────────────────
    _es_refresh(es_url)

    for winner in last_per_source.values():
        doc_id = winner["doc_id"]
        version = winner["version"]
        source_id = winner["source_id"]

        hits = _es_chunks(es_url, doc_id)
        assert hits, f"no ES chunks for winner {doc_id} ({source_id} v{version})"

        combined = " ".join(h.get("content") or h.get("text") or "" for h in hits)
        marker = _VERSION_MARKER[version - 1]
        assert marker in combined, (
            f"expected '{marker}' in ES chunks for {source_id}; "
            f"winner is v{version} (doc_id={doc_id}); "
            f"chunk text (first 300 chars): {combined[:300]!r}"
        )
