"""T-RG.1 — RedisCircuit: bounded blast radius for an unreachable Redis.

The breaker is what turns "Redis is down" from a per-request cost into a
one-off cost: once open, calls short-circuit to the fail-soft default with
zero I/O, so the event loop never pays the socket timeout again until the
cooldown lapses.
"""

from __future__ import annotations

import redis as redis_lib

from ragent.clients.redis_guard import UNAVAILABLE, RedisCircuit


class _Clock:
    """Manual clock so cooldown behaviour is asserted without sleeping."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _circuit(clock: _Clock | None = None, *, threshold: int = 3, cooldown: float = 5.0):
    return RedisCircuit(
        "test",
        failure_threshold=threshold,
        cooldown_seconds=cooldown,
        clock=clock or _Clock(),
    )


def _boom() -> None:
    raise redis_lib.ConnectionError("connection refused")


# --- closed circuit ----------------------------------------------------


def test_closed_circuit_returns_the_call_result() -> None:
    assert _circuit().call("get", lambda: "value") == "value"


def test_redis_error_is_swallowed_and_reported_as_unavailable() -> None:
    assert _circuit().call("get", _boom) is UNAVAILABLE


def test_unavailable_is_falsy_so_plain_truth_checks_still_read_naturally() -> None:
    assert not UNAVAILABLE


def test_non_redis_errors_propagate() -> None:
    """A bug in our own lambda must not be disguised as a Redis outage."""

    def _bug() -> None:
        raise ValueError("this is our bug, not redis being down")

    circuit = _circuit()
    try:
        circuit.call("get", _bug)
    except ValueError:
        pass
    else:  # pragma: no cover - the assert below reports the real failure
        raise AssertionError("ValueError should have propagated")
    assert circuit.state == "closed"  # and must not count toward the trip


# --- tripping ----------------------------------------------------------


def test_circuit_opens_after_the_failure_threshold() -> None:
    circuit = _circuit(threshold=3)
    for _ in range(2):
        circuit.call("get", _boom)
    assert circuit.state == "closed"
    circuit.call("get", _boom)
    assert circuit.state == "open"


def test_open_circuit_does_not_touch_redis_at_all() -> None:
    """The whole point: an open circuit costs zero I/O, so zero latency."""
    circuit = _circuit(threshold=1)
    circuit.call("get", _boom)
    assert circuit.state == "open"

    calls: list[int] = []

    def _tracked() -> str:
        calls.append(1)
        return "value"

    assert circuit.call("get", _tracked) is UNAVAILABLE
    assert calls == []  # never invoked


def test_a_success_resets_the_failure_run() -> None:
    """Intermittent blips must not accumulate into a trip across minutes."""
    circuit = _circuit(threshold=3)
    circuit.call("get", _boom)
    circuit.call("get", _boom)
    circuit.call("get", lambda: "value")
    circuit.call("get", _boom)
    circuit.call("get", _boom)
    assert circuit.state == "closed"


def test_watch_error_never_trips_the_circuit() -> None:
    """WatchError subclasses RedisError but means Redis is HEALTHY.

    It is the optimistic-lock conflict PatCache.put relies on; counting it as
    a connectivity failure would let ordinary write contention open the
    circuit and disable the PAT cache.
    """
    circuit = _circuit(threshold=1)

    def _contended() -> None:
        raise redis_lib.WatchError("watched key changed")

    try:
        circuit.call("put", _contended)
    except redis_lib.WatchError:
        pass
    else:  # pragma: no cover
        raise AssertionError("WatchError should propagate to the caller")
    assert circuit.state == "closed"


# --- recovery ----------------------------------------------------------


def test_open_circuit_stays_open_until_the_cooldown_lapses() -> None:
    clock = _Clock()
    circuit = _circuit(clock, threshold=1, cooldown=5.0)
    circuit.call("get", _boom)

    clock.advance(4.9)
    assert circuit.call("get", lambda: "value") is UNAVAILABLE


def test_half_open_probe_closes_the_circuit_on_success() -> None:
    clock = _Clock()
    circuit = _circuit(clock, threshold=1, cooldown=5.0)
    circuit.call("get", _boom)

    clock.advance(5.0)
    assert circuit.call("get", lambda: "value") == "value"
    assert circuit.state == "closed"


def test_failed_half_open_probe_restarts_the_cooldown() -> None:
    """A still-dead Redis must not be probed on every subsequent request."""
    clock = _Clock()
    circuit = _circuit(clock, threshold=1, cooldown=5.0)
    circuit.call("get", _boom)

    clock.advance(5.0)
    assert circuit.call("get", _boom) is UNAVAILABLE
    assert circuit.state == "open"

    calls: list[int] = []
    clock.advance(1.0)  # well inside the restarted cooldown
    circuit.call("get", lambda: calls.append(1))
    assert calls == []
