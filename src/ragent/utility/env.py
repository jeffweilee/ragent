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
    return raw.lower() in ("1", "true", "yes", "on")


def float_env(var: str, default: float) -> float:
    raw = os.environ.get(var)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"[ragent] {var!r} must be a float, got {raw!r}", file=sys.stderr)
        sys.exit(1)


def float_env_or(passed: float | None, var: str, default: float) -> float:
    """Caller-arg → env-var → default, distinguishing `None` (unset) from `0`.

    The `passed or float_env(...)` shorthand swallows an explicit `0` because
    `0.0` is falsy; this resolver honours the explicit value. Inherits
    `float_env`'s `[ragent] X must be a float` exit path for malformed env.
    """
    if passed is not None:
        return passed
    return float_env(var, default)


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


def parse_sentinel_hosts(hosts_raw: str) -> list[tuple[str, int]]:
    """Parse comma-separated 'host:port' sentinel entries.

    Raises SystemExit on malformed input so callers get the same fail-fast
    behaviour as `require()` / `int_env()`.
    """
    result = []
    for entry in hosts_raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            print(
                f"[ragent] sentinel host entry {entry!r} missing port (expected host:port)",
                file=sys.stderr,
            )
            sys.exit(1)
        host, _, raw_port = entry.rpartition(":")
        try:
            result.append((host, int(raw_port)))
        except ValueError:
            print(
                f"[ragent] sentinel host entry {entry!r} has non-integer port {raw_port!r}",
                file=sys.stderr,
            )
            sys.exit(1)
    return result
