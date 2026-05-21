"""Startup guard: validates auth-mode coherence and safety constraints.

Three supported auth modes (mirrors `composition.py` lines 311-313 where
the JWT verifier is conditionally built):

  A. RAGENT_AUTH_DISABLED=true                                — open auth
  B. AUTH_DISABLED=false, TRUST_X_USER_ID_HEADER=true         — JWT bypassed
  C. AUTH_DISABLED=false, TRUST_X_USER_ID_HEADER=false        — OIDC JWT

Modes A and B trust an unverified header; they are dev-only. Mode A
additionally requires loopback bind because the endpoint exposes no auth
surface at all. Mode C tolerates any env / any bind because the JWT is
the auth surface.
"""

from __future__ import annotations

import os
import sys

from ragent.utility.env import bool_env

_VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


def enforce() -> None:
    env = os.environ.get("RAGENT_ENV", "dev")
    auth_disabled = bool_env("RAGENT_AUTH_DISABLED", False)
    trust_header = bool_env("RAGENT_TRUST_X_USER_ID_HEADER", False)
    host = os.environ.get("RAGENT_HOST", "127.0.0.1")
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()

    if auth_disabled:
        if env != "dev":
            _exit(
                f"RAGENT_AUTH_DISABLED=true requires RAGENT_ENV=dev "
                f"(open auth is dev-only, got '{env}')"
            )
        if host != "127.0.0.1":
            _exit(
                f"RAGENT_AUTH_DISABLED=true requires RAGENT_HOST=127.0.0.1 "
                f"(open auth must bind loopback, got '{host}')"
            )
    elif trust_header:
        if env != "dev":
            _exit(
                f"RAGENT_TRUST_X_USER_ID_HEADER=true requires RAGENT_ENV=dev "
                f"(dev override, got '{env}')"
            )
    else:
        if not os.environ.get("OIDC_DOMAIN"):
            _exit(
                "OIDC mode requires OIDC_DOMAIN (set RAGENT_AUTH_DISABLED=true for dev open-auth)"
            )
        if not os.environ.get("OIDC_AUDIENCE"):
            _exit("OIDC mode requires OIDC_AUDIENCE")

    if log_level not in _VALID_LOG_LEVELS:
        _exit(f"LOG_LEVEL '{log_level}' is invalid; must be one of {sorted(_VALID_LOG_LEVELS)}")

    from ragent.pipelines.factory import validate_chunk_config

    try:
        validate_chunk_config()
    except RuntimeError as exc:
        _exit(str(exc))


def _exit(message: str) -> None:
    print(f"[ragent startup guard] {message}", file=sys.stderr)
    sys.exit(1)
