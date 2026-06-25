"""Shared RUN_ERROR construction for Agent implementations.

An exception carrying `error_code` was deliberately raised by a caller/client
to be shown to the end user (e.g. a classified upstream failure) — its
`str()` is authored text, safe to expose. Anything else is an unexpected
internal bug: its `str()` is Python/library-native and may contain whatever a
buggy code path happened to interpolate, so it must never reach the client.
It is logged here instead, with the full traceback, for server-side debugging.
"""

from __future__ import annotations

import logging

from ..events import RunErrorEvent

_INTERNAL_ERROR_CODE = "INTERNAL_ERROR"
_INTERNAL_ERROR_MESSAGE = "internal error"


def run_error_event(
    exc: Exception, *, run_id: str, thread_id: str, logger: logging.Logger
) -> RunErrorEvent:
    error_code = getattr(exc, "error_code", None)
    if error_code is not None:
        return RunErrorEvent(message=str(exc), code=error_code, run_id=run_id, thread_id=thread_id)
    logger.error("twp_ai.agent.unhandled_error", exc_info=exc)
    return RunErrorEvent(
        message=_INTERNAL_ERROR_MESSAGE,
        code=_INTERNAL_ERROR_CODE,
        run_id=run_id,
        thread_id=thread_id,
    )
