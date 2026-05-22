"""T-EM-R.9 — backfill worker: scroll stable, mget candidate, embed+write missing.

Tests cover _run_backfill (synchronous inner loop) directly.
"""

from unittest.mock import MagicMock

from ragent.clients.embedding_model_config import EmbeddingModelConfig


def _model(name: str, dim: int) -> EmbeddingModelConfig:
    return EmbeddingModelConfig(name=name, dim=dim, api_url="http://test", model_arg=name)


def _scroll_page(ids: list[str], scroll_id: str = "scroll-1") -> dict:
    return {
        "_scroll_id": scroll_id,
        "hits": {
            "hits": [
                {
                    "_id": id_,
                    "_source": {
                        "content": f"text {id_}",
                        "title": f"Title {id_}",
                        "document_id": f"doc-{id_}",
                    },
                }
                for id_ in ids
            ]
        },
    }


def _mget_response(ids: list[str], found: list[bool]) -> dict:
    return {"docs": [{"_id": id_, "found": f} for id_, f in zip(ids, found, strict=True)]}


def _es_mock(first_page_ids, *, scroll_pages=None, mget_found=None):
    es = MagicMock()
    es.search.return_value = _scroll_page(first_page_ids)
    terminal = _scroll_page([])
    es.scroll.side_effect = list(scroll_pages or []) + [terminal]
    all_ids = first_page_ids[:]
    for page in scroll_pages or []:
        all_ids += [h["_id"] for h in page["hits"]["hits"]]
    if mget_found is None:
        mget_found = [False] * len(all_ids)
    # mget called once per page; split found list per page
    page_sizes = [len(first_page_ids)] + [len(p["hits"]["hits"]) for p in (scroll_pages or [])]
    idx = 0
    per_page_found = []
    for sz in page_sizes:
        if sz == 0:
            break
        per_page_found.append(mget_found[idx : idx + sz])
        idx += sz
    es.mget.side_effect = [
        _mget_response(first_page_ids[i : i + sz] if i == 0 else [], found_batch)
        for i, (sz, found_batch) in enumerate(zip(page_sizes, per_page_found, strict=True))
    ]
    # rebuild mget side_effect properly
    es.mget.side_effect = None
    mget_calls = []
    offset = 0
    for sz in page_sizes:
        if sz == 0:
            break
        page_ids = all_ids[offset : offset + sz]
        page_found = mget_found[offset : offset + sz]
        mget_calls.append(_mget_response(page_ids, page_found))
        offset += sz
    es.mget.side_effect = mget_calls
    es.bulk.return_value = {"errors": False, "items": []}
    return es


def test_backfill_scrolls_and_embeds_missing_chunks() -> None:
    """All chunks missing → all embedded and bulk-written to candidate_index."""
    from ragent.workers.backfill import _run_backfill

    model = _model("bge-m3-v2", 768)
    es = MagicMock()
    es.search.return_value = _scroll_page(["id-1", "id-2"])
    es.scroll.return_value = _scroll_page([])
    es.mget.return_value = _mget_response(["id-1", "id-2"], [False, False])
    es.bulk.return_value = {"errors": False, "items": []}

    embed_calls: list[list[str]] = []

    def embed_fn(m, texts):
        embed_calls.append(list(texts))
        return [[0.5] * m.dim for _ in texts]

    total = _run_backfill(
        es=es,
        stable_index="chunks_v1",
        candidate_index="chunks_v2",
        embed_fn=embed_fn,
        candidate_model=model,
    )

    assert total == 2
    assert embed_calls == [["Title id-1\n\ntext id-1", "Title id-2\n\ntext id-2"]]
    es.bulk.assert_called_once()
    assert es.bulk.call_args.kwargs["index"] == "chunks_v2"


def test_backfill_skips_already_covered_chunks() -> None:
    """Chunks found in candidate_index (mget found=True) are not re-embedded."""
    from ragent.workers.backfill import _run_backfill

    model = _model("bge-m3-v2", 768)
    es = MagicMock()
    es.search.return_value = _scroll_page(["id-1", "id-2"])
    es.scroll.return_value = _scroll_page([])
    # id-1 already covered; id-2 missing
    es.mget.return_value = _mget_response(["id-1", "id-2"], [True, False])
    es.bulk.return_value = {"errors": False, "items": []}

    embed_calls: list[list[str]] = []

    def embed_fn(m, texts):
        embed_calls.append(list(texts))
        return [[0.1] * m.dim for _ in texts]

    total = _run_backfill(
        es=es,
        stable_index="chunks_v1",
        candidate_index="chunks_v2",
        embed_fn=embed_fn,
        candidate_model=model,
    )

    assert total == 1
    assert len(embed_calls) == 1
    assert len(embed_calls[0]) == 1  # only id-2 embedded
    ops = es.bulk.call_args.kwargs["operations"]
    actions = [ops[i] for i in range(0, len(ops), 2)]
    assert all(a["index"]["_id"] == "id-2" for a in actions)


def test_backfill_noop_when_stable_index_empty() -> None:
    """Empty stable_index → no embed, no bulk calls."""
    from ragent.workers.backfill import _run_backfill

    model = _model("bge-m3-v2", 768)
    es = MagicMock()
    es.search.return_value = {"_scroll_id": "scroll-1", "hits": {"hits": []}}

    embed_calls: list = []

    total = _run_backfill(
        es=es,
        stable_index="chunks_v1",
        candidate_index="chunks_v2",
        embed_fn=lambda m, t: embed_calls.append(t) or [],
        candidate_model=model,
    )

    assert total == 0
    assert embed_calls == []
    es.bulk.assert_not_called()
    es.clear_scroll.assert_called_once()


def test_backfill_processes_multiple_scroll_pages() -> None:
    """Two non-empty pages → two embed+bulk calls, total matches all missing chunks."""
    from ragent.workers.backfill import _run_backfill

    model = _model("bge-m3-v2", 768)
    es = MagicMock()
    es.search.return_value = _scroll_page(["id-1", "id-2"])
    es.scroll.side_effect = [_scroll_page(["id-3"]), _scroll_page([])]
    es.mget.side_effect = [
        _mget_response(["id-1", "id-2"], [False, False]),
        _mget_response(["id-3"], [False]),
    ]
    es.bulk.return_value = {"errors": False, "items": []}

    total = _run_backfill(
        es=es,
        stable_index="chunks_v1",
        candidate_index="chunks_v2",
        embed_fn=lambda m, t: [[0.1] * m.dim for _ in t],
        candidate_model=model,
    )

    assert total == 3
    assert es.bulk.call_count == 2


def test_backfill_increments_chunks_metric() -> None:
    """ragent_backfill_chunks_total incremented by the number of chunks written."""
    from ragent.bootstrap.metrics import ragent_backfill_chunks_total
    from ragent.workers.backfill import _run_backfill

    model = _model("bge-m3-v2", 768)
    es = MagicMock()
    es.search.return_value = _scroll_page(["id-1", "id-2"])
    es.scroll.return_value = _scroll_page([])
    es.mget.return_value = _mget_response(["id-1", "id-2"], [False, False])
    es.bulk.return_value = {"errors": False, "items": []}

    before = ragent_backfill_chunks_total._value.get()

    _run_backfill(
        es=es,
        stable_index="chunks_v1",
        candidate_index="chunks_v2",
        embed_fn=lambda m, t: [[0.1] * m.dim for _ in t],
        candidate_model=model,
    )

    assert ragent_backfill_chunks_total._value.get() - before == 2
