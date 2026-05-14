"""Typed env-var accessors used by composition + components."""

from __future__ import annotations

import os
import sys


def require(var: str) -> str:
    val = os.environ.get(var, "")
    if not val:
        print(f"[ragent] required env var {var!r} is not set", file=sys.stderr)
        sys.exit(1)
    return val


def int_env(var: str, default: int) -> int:
    raw = os.environ.get(var)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"[ragent] {var!r} must be an integer, got {raw!r}", file=sys.stderr)
        sys.exit(1)


def bool_env(var: str, default: bool) -> bool:
    raw = os.environ.get(var)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes")


def float_env(var: str, default: float) -> float:
    raw = os.environ.get(var)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"[ragent] {var!r} must be a float, got {raw!r}", file=sys.stderr)
        sys.exit(1)


def optional_float_env(var: str) -> float | None:
    raw = os.environ.get(var)
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        print(f"[ragent] {var!r} must be a float, got {raw!r}", file=sys.stderr)
        sys.exit(1)


def str_env(var: str, default: str) -> str:
    raw = os.environ.get(var)
    return default if raw is None else raw


def list_env(var: str) -> list[str]:
    raw = os.environ.get(var, "")
    return [item.strip() for item in raw.split(",") if item.strip()]
