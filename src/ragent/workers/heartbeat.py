"""T3.2b / TA.10 — Worker heartbeat: periodic updated_at refresh (B16).

`run_heartbeat` runs in a plain threading.Thread. It maintains one asyncio
event loop for its entire lifetime so the async repo pool is reused across ticks.
"""

from __future__ import annotations

import asyncio
import os
import threading
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_DEFAULT_INTERVAL = float(os.environ.get("WORKER_HEARTBEAT_INTERVAL_SECONDS", "30"))


def run_heartbeat(
    document_id: str,
    repo: Any,
    stop: threading.Event,
    interval: float = _DEFAULT_INTERVAL,
) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        while not stop.wait(timeout=interval):
            try:
                loop.run_until_complete(repo.update_heartbeat(document_id))
            except Exception as exc:
                logger.warning("heartbeat.update_failed", document_id=document_id, error=str(exc))
    finally:
        loop.close()
