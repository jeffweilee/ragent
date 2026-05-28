"""T-EM-R.9 — Backfill worker: embed missing chunks into candidate_index.

Per-batch algorithm (batch_size chunks):
1. ES scroll stable_index — fetch _id + _source (all fields except embedding)
2. _mget candidate_index — keep only _ids where found=False
3. Embed missing chunks with candidate model (text field)
4. ES bulk index → candidate_index with field "embedding"
5. Repeat until scroll exhausted

The task is idempotent: _mget skip prevents double-embedding chunks already
written by dual-write during the CANDIDATE window.
"""

from __future__ import annotations

import structlog
from anyio import to_thread

from ragent.bootstrap.broker import broker
from ragent.bootstrap.metrics import ragent_backfill_chunks_total

logger = structlog.get_logger(__name__)

_BATCH_SIZE = 256
_SCROLL_TTL = "2m"


def _run_backfill(
    *,
    es,
    stable_index: str,
    candidate_index: str,
    embed_fn,
    candidate_model,
    batch_size: int = _BATCH_SIZE,
) -> int:
    """Synchronous inner loop — runs in a thread via anyio.to_thread.run_sync.

    Returns total number of chunks written to candidate_index.
    """
    total = 0
    resp = es.search(
        index=stable_index,
        body={"query": {"match_all": {}}, "_source": {"excludes": ["embedding"]}},
        size=batch_size,
        scroll=_SCROLL_TTL,
    )
    scroll_id = resp["_scroll_id"]
    hits = resp["hits"]["hits"]

    try:
        while hits:
            ids = [h["_id"] for h in hits]
            mget = es.mget(index=candidate_index, body={"ids": ids})
            missing = [h for h, doc in zip(hits, mget["docs"], strict=True) if not doc.get("found")]

            if missing:
                texts = [h["_source"].get("text") or "" for h in missing]
                vectors = embed_fn(candidate_model, texts)
                ops = []
                for hit, vec in zip(missing, vectors, strict=True):
                    ops.append({"index": {"_id": hit["_id"]}})
                    ops.append({**hit["_source"], "embedding": vec})
                es.bulk(index=candidate_index, operations=ops)
                total += len(missing)
                ragent_backfill_chunks_total.inc(len(missing))

            resp = es.scroll(scroll_id=scroll_id, scroll=_SCROLL_TTL)
            scroll_id = resp["_scroll_id"]
            hits = resp["hits"]["hits"]
    finally:
        es.clear_scroll(scroll_id=scroll_id)

    return total


@broker.task("ingest.backfill_candidate")
async def backfill_candidate_task(stable_index: str, candidate_index: str) -> None:
    from ragent.bootstrap.composition import get_container

    try:
        container = get_container()
        es = container.es_client
        registry = container.embedding_registry
        await registry.refresh()

        write_models = list(registry.write_models())
        if len(write_models) < 2:
            logger.info("backfill.skipped.not_in_candidate_state")
            return

        candidate_model = write_models[1]
        embed_fn = container.embed_fn

        total = await to_thread.run_sync(
            lambda: _run_backfill(
                es=es,
                stable_index=stable_index,
                candidate_index=candidate_index,
                embed_fn=embed_fn,
                candidate_model=candidate_model,
            )
        )
        logger.info(
            "backfill.complete",
            stable_index=stable_index,
            candidate_index=candidate_index,
            chunks_written=total,
        )
    except Exception as exc:
        logger.error(
            "backfill.failed",
            stable_index=stable_index,
            candidate_index=candidate_index,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        raise
