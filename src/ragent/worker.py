"""T7.5e — Worker process entrypoint: python -m ragent.worker (B30)."""

from __future__ import annotations

# load_env() must run before any ragent.* imports so that module-level
# os.environ.get() calls see .env values.
from ragent.config import load_env

from ragent.bootstrap.guard import enforce
from ragent.bootstrap.init_schema import init_schema
from ragent.bootstrap.logging_config import configure_logging
from ragent.bootstrap.telemetry import setup_tracing

if __name__ == "__main__":
    load_env()
    enforce()
    configure_logging("ragent-worker")
    setup_tracing("ragent-worker")
    init_schema()

    # Import task modules so @broker.task decorators register before broker starts
    import ragent.workers.ingest  # noqa: F401
    from ragent.bootstrap.broker import broker

    broker.run()
