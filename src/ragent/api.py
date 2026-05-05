"""T7.5d — API process entrypoint: python -m ragent.api (B30)."""

from __future__ import annotations

import os

import uvicorn

from ragent.bootstrap.app import create_app

if __name__ == "__main__":
    host = os.environ.get("RAGENT_HOST", "127.0.0.1")
    port = int(os.environ.get("RAGENT_PORT", "8000"))
    log_level = os.environ.get("LOG_LEVEL", "INFO").lower()

    app = create_app()
    uvicorn.run(app, host=host, port=port, log_level=log_level)
