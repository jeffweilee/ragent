"""T7.5e — Worker process entrypoint: python -m ragent.worker (B30)."""

from __future__ import annotations

from ragent.bootstrap.guard import enforce
from ragent.bootstrap.init_schema import init_schema
from ragent.bootstrap.telemetry import setup_tracing

if __name__ == "__main__":
    enforce()
    setup_tracing("ragent-worker")
    init_schema()

    # Import task modules so @broker.task decorators register before broker starts
    import ragent.workers.ingest  # noqa: F401
    from ragent.bootstrap.broker import broker

    broker.run()
