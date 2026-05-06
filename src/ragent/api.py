"""T7.5d — API process entrypoint: python -m ragent.api (B30)."""

from __future__ import annotations

# load_env() must run before any ragent.* imports so that module-level
# os.environ.get() calls (e.g. in schemas/chat.py) see .env values.
from ragent.config import load_env

import uvicorn

if __name__ == "__main__":
    settings = load_env()

    from ragent.bootstrap.app import create_app

    app = create_app()
    uvicorn.run(app, host=settings.ragent_host, port=settings.ragent_port, log_level=settings.log_level.lower())
