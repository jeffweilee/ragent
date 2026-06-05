"""Startup guard: validates RAGENT_AUTH_MODE coherence and safety constraints.

Four supported auth modes (see auth_mode.py):

  none             — no header required; dev only
  user_header      — trust X-User-Id header, no JWT; dev only
  jwt_header       — OIDC JWT only; no env restriction
  jwt_prefer_header— JWT with X-User-Id fallback; dev only

JWT verification flags (both default true; require RAGENT_ENV=dev when false):
  RAGENT_JWT_VERIFY_AUD — enforce audience claim
  RAGENT_JWT_VERIFY_EXP — enforce expiry claim
"""

from __future__ import annotations

import os
import sys

from ragent.bootstrap.auth_mode import AuthMode, parse_auth_mode
from ragent.utility.env import bool_env as _bool_env

_VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


def enforce() -> None:
    try:
        mode = parse_auth_mode()
    except ValueError as exc:
        _exit(str(exc))
        return  # unreachable; satisfies type checker

    env = os.environ.get("RAGENT_ENV", "dev")

    if mode in (AuthMode.none, AuthMode.user_header, AuthMode.jwt_prefer_header) and env != "dev":
        _exit(f"RAGENT_AUTH_MODE={mode!r} requires RAGENT_ENV=dev (dev-only mode, got '{env}')")

    if mode in (AuthMode.jwt_header, AuthMode.jwt_prefer_header):
        if not os.environ.get("OIDC_DOMAIN"):
            _exit(f"RAGENT_AUTH_MODE={mode!r} requires OIDC_DOMAIN")
        if not os.environ.get("OIDC_AUDIENCE"):
            _exit(f"RAGENT_AUTH_MODE={mode!r} requires OIDC_AUDIENCE")

    if not _bool_env("RAGENT_JWT_VERIFY_AUD", True) and env != "dev":
        _exit("RAGENT_JWT_VERIFY_AUD=false requires RAGENT_ENV=dev")
    if not _bool_env("RAGENT_JWT_VERIFY_EXP", True) and env != "dev":
        _exit("RAGENT_JWT_VERIFY_EXP=false requires RAGENT_ENV=dev")

    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    if log_level not in _VALID_LOG_LEVELS:
        _exit(f"LOG_LEVEL '{log_level}' is invalid; must be one of {sorted(_VALID_LOG_LEVELS)}")

    from ragent.pipelines.ingest import validate_chunk_config

    try:
        validate_chunk_config()
    except RuntimeError as exc:
        _exit(str(exc))


def _exit(message: str) -> None:
    print(f"[ragent startup guard] {message}", file=sys.stderr)
    sys.exit(1)
