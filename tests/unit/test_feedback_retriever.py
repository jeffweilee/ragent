"""T-FB.7 — _FeedbackMemoryRetriever: kNN + Wilson + decay + single-terms-lookup."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from ragent.pipelines.chat import _FeedbackMemoryRetriever


def _hit(source_app: str, source_id: str, vote: int, ts: datetime) -> dict:
    return {
        "_source": {
            "source_app": source_app,
            "source_id": source_id,
            "vote": vote,
            "ts": ts.isoformat().replace("+00:00", "Z"),
        }
    }


def _knn_response(hits: list[dict]) -> dict:
    return {"hits": {"hits": hits}}


def _chunk_hit(document_id: str, chunk_id: str, text: str = "chunk text") -> dict:
    return {
        "_source": {
            "document_id": document_id,
            "chunk_id": chunk_id,
            "source_app": "confluence",
            "text": text,
        }
    }


def _make_retriever(min_votes: int = 3, half_life_days: int = 14) -> tuple:
    es = MagicMock()
    doc_repo = MagicMock()
    retriever = _FeedbackMemoryRetriever(
        es_client=es,
        doc_repo=doc_repo,
        min_votes=min_votes,
        half_life_days=half_life_days,
    )
    return retriever, es, doc_repo


def _fresh_ts(days_ago: int = 0) -> datetime:
    return datetime.now(UTC) - timedelta(days=days_ago)


@patch("anyio.from_thread.run")
def test_empty_knn_returns_empty(from_thread):
    r, es, _ = _make_retriever()
    es.search = MagicMock(return_value=_knn_response([]))
    out = r.run(query_embedding=[0.1] * 1024)
    assert out["documents"] == []
    from_thread.assert_not_called()  # short-circuit before MariaDB lookup


@patch("anyio.from_thread.run")
def test_below_min_votes_gate_filters(from_thread):
    r, es, _ = _make_retriever(min_votes=3)
    es.search = MagicMock(
        return_value=_knn_response(
            [
                _hit("confluence", "DOC-A", 1, _fresh_ts()),
                _hit("confluence", "DOC-A", 1, _fresh_ts()),  # only 2 likes total
            ]
        )
    )
    out = r.run(query_embedding=[0.1] * 1024)
    assert out["documents"] == []
    from_thread.assert_not_called()


@patch("anyio.from_thread.run")
def test_happy_path_returns_chunks_with_inherited_scores(from_thread):
    r, es, repo = _make_retriever(min_votes=3)
    es.search = MagicMock(
        side_effect=[
            _knn_response(
                [
                    _hit("confluence", "DOC-A", 1, _fresh_ts(1)),
                    _hit("confluence", "DOC-A", 1, _fresh_ts(1)),
                    _hit("confluence", "DOC-A", 1, _fresh_ts(1)),  # 3 likes
                ]
            ),
            _knn_response([_chunk_hit("DOCID01ABC", "CHUNK01")]),
        ]
    )
    from_thread.return_value = {("confluence", "DOC-A"): "DOCID01ABC"}
    out = r.run(query_embedding=[0.1] * 1024)
    assert len(out["documents"]) == 1
    doc = out["documents"][0]
    assert doc.meta["document_id"] == "DOCID01ABC"
    assert doc.score is not None and 0.0 < doc.score <= 1.0


@patch("anyio.from_thread.run")
def test_chunks_lookup_is_single_es_query(from_thread):
    """Even with N surviving sources, chunks_v1 is hit exactly once (terms)."""
    r, es, repo = _make_retriever(min_votes=3)
    hits = []
    for letter in "ABCDE":
        for _ in range(3):
            hits.append(_hit("confluence", f"DOC-{letter}", 1, _fresh_ts(1)))
    es.search = MagicMock(
        side_effect=[
            _knn_response(hits),
            _knn_response([_chunk_hit(f"DOCID0{letter}", f"CK0{letter}") for letter in "ABCDE"]),
        ]
    )
    from_thread.return_value = {
        ("confluence", f"DOC-{letter}"): f"DOCID0{letter}" for letter in "ABCDE"
    }
    out = r.run(query_embedding=[0.1] * 1024)
    # Two ES calls total: one kNN on feedback_v1, one terms on chunks_v1.
    assert es.search.call_count == 2
    # Second call is the terms query
    second_call = es.search.call_args_list[1]
    assert second_call.kwargs["index"] == "chunks_v1"
    assert "terms" in str(second_call.kwargs["body"])
    assert len(out["documents"]) == 5


@patch("anyio.from_thread.run")
def test_decay_lowers_score_for_older_feedback(from_thread):
    r, es, _ = _make_retriever(min_votes=3, half_life_days=14)
    # Run 1: fresh feedback
    es.search = MagicMock(
        side_effect=[
            _knn_response([_hit("confluence", "DOC-A", 1, _fresh_ts(0))] * 3),
            _knn_response([_chunk_hit("DOCID01", "CK01")]),
        ]
    )
    from_thread.return_value = {("confluence", "DOC-A"): "DOCID01"}
    fresh = r.run(query_embedding=[0.1] * 1024)["documents"][0].score

    # Run 2: 28-day-old feedback (2 half-lives)
    es.search = MagicMock(
        side_effect=[
            _knn_response([_hit("confluence", "DOC-A", 1, _fresh_ts(28))] * 3),
            _knn_response([_chunk_hit("DOCID01", "CK01")]),
        ]
    )
    old = r.run(query_embedding=[0.1] * 1024)["documents"][0].score
    assert old < fresh / 2  # 2 half-lives → < 25% of fresh score


@patch("anyio.from_thread.run")
def test_dislikes_reduce_wilson_score(from_thread):
    r, es, _ = _make_retriever(min_votes=3)
    # 3 likes vs 2 likes + 1 dislike on same total = lower Wilson
    es.search = MagicMock(
        side_effect=[
            _knn_response([_hit("confluence", "DOC-A", 1, _fresh_ts(0))] * 3),
            _knn_response([_chunk_hit("DOCID01", "CK01")]),
        ]
    )
    from_thread.return_value = {("confluence", "DOC-A"): "DOCID01"}
    pure_likes = r.run(query_embedding=[0.1] * 1024)["documents"][0].score

    es.search = MagicMock(
        side_effect=[
            _knn_response(
                [
                    _hit("confluence", "DOC-A", 1, _fresh_ts(0)),
                    _hit("confluence", "DOC-A", 1, _fresh_ts(0)),
                    _hit("confluence", "DOC-A", -1, _fresh_ts(0)),
                ]
            ),
            _knn_response([_chunk_hit("DOCID01", "CK01")]),
        ]
    )
    mixed = r.run(query_embedding=[0.1] * 1024)["documents"][0].score
    assert mixed < pure_likes


@patch("anyio.from_thread.run")
def test_optional_source_app_filter_propagates(from_thread):
    r, es, _ = _make_retriever()
    es.search = MagicMock(return_value=_knn_response([]))
    r.run(query_embedding=[0.1] * 1024, filters={"source_app": "confluence"})
    body = es.search.call_args.kwargs["body"]
    knn_filter = body["knn"]["filter"]["bool"]["filter"]
    assert {"term": {"source_app": "confluence"}} in knn_filter


@patch("anyio.from_thread.run")
def test_lookup_miss_drops_source(from_thread):
    """If MariaDB has no READY row for a (source_app, source_id), drop it (B36 alignment)."""
    r, es, _ = _make_retriever(min_votes=3)
    es.search = MagicMock(
        side_effect=[
            _knn_response(
                [
                    _hit("confluence", "DOC-A", 1, _fresh_ts(0)),
                    _hit("confluence", "DOC-A", 1, _fresh_ts(0)),
                    _hit("confluence", "DOC-A", 1, _fresh_ts(0)),
                ]
            ),
            _knn_response([]),  # no chunks (no current READY)
        ]
    )
    from_thread.return_value = {}  # MariaDB returned no mapping
    out = r.run(query_embedding=[0.1] * 1024)
    assert out["documents"] == []
    # Only one ES call: short-circuit before chunks lookup when mapping is empty
    assert es.search.call_count == 1


# --- T-FB review-fix: filter bridge unit ---


def test_scope_from_haystack_filters_extracts_source_app_and_meta():
    """PR #80 review (codex P1): run_retrieval must bridge Haystack composite
    filters (the shape build_es_filters emits) into the flat shape the
    feedback retriever consumes — otherwise the chat-side scope is silently
    bypassed by the feedback boost."""
    from ragent.pipelines.chat import _scope_from_haystack_filters

    # leaf shape (single filter)
    leaf = {"field": "source_app", "operator": "==", "value": "confluence"}
    assert _scope_from_haystack_filters(leaf) == {"source_app": "confluence"}

    # composite AND shape (both filters)
    composite = {
        "operator": "AND",
        "conditions": [
            {"field": "source_app", "operator": "==", "value": "confluence"},
            {"field": "source_meta", "operator": "==", "value": "engineering"},
        ],
    }
    assert _scope_from_haystack_filters(composite) == {
        "source_app": "confluence",
        "source_meta": "engineering",
    }

    # None / empty stays None
    assert _scope_from_haystack_filters(None) is None
    assert _scope_from_haystack_filters({}) is None
