"""Typed env-var accessors."""

from __future__ import annotations

import os
import sys


def int_env(var: str, default: int) -> int:
    raw = os.environ.get(var)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"[mcp-hub] {var!r} must be an integer, got {raw!r}", file=sys.stderr)
        sys.exit(1)


def bool_env(var: str, default: bool) -> bool:
    raw = os.environ.get(var)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def str_env(var: str, default: str) -> str:
    raw = os.environ.get(var)
    return default if raw is None else raw
