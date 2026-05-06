"""T7.5e — Worker process entrypoint: python -m ragent.worker (B30)."""

from __future__ import annotations

from ragent.bootstrap.guard import enforce
from ragent.bootstrap.init_schema import init_schema
from ragent.bootstrap.logging_config import configure_logging
from ragent.bootstrap.telemetry import setup_tracing

if __name__ == "__main__":
    enforce()
    configure_logging("ragent-worker")
    setup_tracing("ragent-worker")
    init_schema()

    from taskiq.cli.worker.args import WorkerArgs
    from taskiq.cli.worker.run import start_listen

    start_listen(
        WorkerArgs(
            broker="ragent.bootstrap.broker:broker",
            modules=["ragent.workers.ingest"],
        ),
    )
