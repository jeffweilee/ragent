"""Shared test fixtures/helpers for the twp-ai test suite."""

from __future__ import annotations

import contextlib
import io
import logging
from collections.abc import Callable, Iterator

import pytest


@pytest.fixture
def captured_logger_text() -> Callable[[str], contextlib.AbstractContextManager[io.StringIO]]:
    """Capture a named stdlib logger's formatted output directly.

    Bypasses pytest's ``caplog``/root-logger capture, which is empirically
    flaky under coverage instrumentation when many tests share a session
    (see docs/00_journal.md 2026-05-20 "Subprocess Isolation" and
    docs/00_rule.md "Test Log Capture").
    """

    @contextlib.contextmanager
    def _capture(logger_name: str) -> Iterator[io.StringIO]:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger(logger_name)
        logger.addHandler(handler)
        try:
            yield stream
        finally:
            logger.removeHandler(handler)

    return _capture


@pytest.fixture
def describe_logger_state() -> Callable[[str], str]:
    """Snapshot a logger's capture-relevant state for a failed-assertion message.

    Diagnostic aid for the CI-only flake on the two unclassified-error
    server-side-logging tests: a directly attached handler still saw no
    output in CI, so this surfaces whether the logger itself is disabled,
    above-level, or globally suppressed via ``logging.disable()``.
    """

    def _describe(logger_name: str) -> str:
        logger = logging.getLogger(logger_name)
        return (
            f"handlers={logger.handlers!r} disabled={logger.disabled} "
            f"effective_level={logger.getEffectiveLevel()} "
            f"manager_disable={logging.Logger.manager.disable}"
        )

    return _describe
