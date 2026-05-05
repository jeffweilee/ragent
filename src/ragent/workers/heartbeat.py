"""T3.2b — Worker heartbeat: periodic updated_at refresh to prevent stale-sweep (B16)."""

from __future__ import annotations

import os
import threading
from typing import Any

_DEFAULT_INTERVAL = float(os.environ.get("WORKER_HEARTBEAT_INTERVAL_SECONDS", "30"))


def run_heartbeat(
    document_id: str,
    repo: Any,
    stop: threading.Event,
    interval: float = _DEFAULT_INTERVAL,
) -> None:
    while not stop.wait(timeout=interval):
        repo.update_heartbeat(document_id)
