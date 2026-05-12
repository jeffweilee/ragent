"""Shared helpers for chaos drill cases C1..C6 (spec §3.6.1, B49).

Per-case files under `tests/e2e/test_chaos/` use these to:
  - reset WireMock stubs between cases so injection state from C3/C4/C5/C6
    cannot leak into the next case
  - scrape `chaos_drill_outcome_total{case,outcome}` and record the outcome
    of the case (the gate of B49 requires every case to record its
    `pass`/`fail` so nightly CI can plot drill history)

Fixture-reuse policy (B49):
  - C3 / C4 / C5 / C6 are WireMock-only injections — they reuse the
    session-scoped `running_stack` to avoid the per-case ~30s testcontainer
    boot tax.
  - C1 / C2 deliberately kill the worker process — they use `spawn_module`
    (function-scope) instead because session-scoped subprocesses would be
    left in a dead state for the rest of the session.
"""

from __future__ import annotations

import urllib.request
from collections.abc import Iterator

import httpx
import pytest
from prometheus_client.parser import text_string_to_metric_families

from tests.e2e.conftest import API_URL


@pytest.fixture
def wiremock_reset(wiremock_url: str) -> Iterator[None]:
    """Wipe WireMock dynamic mappings after each chaos case.

    The session-scoped `wiremock_url` fixture loads the default stubs once;
    chaos cases that POST `/__admin/mappings` with `:transient` injections
    must remove those mappings on teardown, otherwise C4 (rerank 5xx)
    leaks into C5 (LLM stream interrupt).
    """
    yield
    _reset_wiremock(wiremock_url)


def _reset_wiremock(wiremock_url: str) -> None:
    """Reset WireMock's mapping store + reapply the session-default stubs.

    `POST /__admin/mappings/reset` reloads from the static files the
    container was started with; here we have no static files (stubs are
    POSTed at session start in `tests/conftest.py`), so this clears
    everything and the session-startup stubs must be reposted.
    """
    req = urllib.request.Request(
        f"{wiremock_url}/__admin/mappings/reset",
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        resp.read()
    from tests.conftest import _configure_wiremock_stubs

    _configure_wiremock_stubs(wiremock_url)


def scrape_chaos_outcomes() -> dict[tuple[str, str], int]:
    """Return `{(case, outcome): count}` from the running API's /metrics.

    Used by per-case tests to assert the C<N> case incremented the
    expected (case, outcome) bucket — see spec §3.6.1 "common acceptance
    asserts" item 4.
    """
    text: str = httpx.get(f"{API_URL}/metrics", timeout=5).text
    out: dict[tuple[str, str], int] = {}
    for family in text_string_to_metric_families(text):
        if family.name != "ragent_chaos_drill_outcome":
            continue
        for sample in family.samples:
            if sample.name != "ragent_chaos_drill_outcome_total":
                continue
            labels = sample.labels
            out[(labels["case"], labels["outcome"])] = int(sample.value)
    return out


__all__ = [
    "scrape_chaos_outcomes",
    "wiremock_reset",
    "_reset_wiremock",
]
