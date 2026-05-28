"""Entry point: python -m twp_ai"""

from __future__ import annotations

import os

import uvicorn

if __name__ == "__main__":
    host = os.environ.get("TWP_HOST", "0.0.0.0")
    port = int(os.environ.get("TWP_PORT", "8001"))
    log_level = os.environ.get("LOG_LEVEL", "INFO").lower()

    uvicorn.run("twp_ai.app:create_app", factory=True, host=host, port=port, log_level=log_level)
