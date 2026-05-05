"""T7.5 — Startup guard: enforces Phase 1 open-auth dev-only constraints (B28)."""

from __future__ import annotations

import os
import sys

_VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


def enforce() -> None:
    env = os.environ.get("RAGENT_ENV", "dev")
    auth_disabled = os.environ.get("RAGENT_AUTH_DISABLED", "").lower()
    host = os.environ.get("RAGENT_HOST", "127.0.0.1")
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()

    if env != "dev":
        _exit(f"RAGENT_ENV must be 'dev' in Phase 1, got '{env}'")
    if auth_disabled != "true":
        _exit("RAGENT_AUTH_DISABLED must be 'true' in Phase 1")
    if host != "127.0.0.1":
        _exit(f"RAGENT_HOST must be '127.0.0.1' in Phase 1 open-auth mode, got '{host}'")
    if log_level not in _VALID_LOG_LEVELS:
        _exit(f"LOG_LEVEL '{log_level}' is invalid; must be one of {sorted(_VALID_LOG_LEVELS)}")


def _exit(message: str) -> None:
    print(f"[ragent startup guard] {message}", file=sys.stderr)
    sys.exit(1)
