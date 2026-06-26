"""T-CAv3N — NatsSessionPublisher: per-user live session-list status over NATS."""

from __future__ import annotations

import asyncio
import json
import threading

from ragent.clients.nats_publisher import NatsSessionPublisher


def test_subject_is_per_user() -> None:
    assert NatsSessionPublisher.subject("session", "alice") == "session.alice.status"
    assert NatsSessionPublisher.subject("session", "bob") != NatsSessionPublisher.subject(
        "session", "alice"
    )


def test_publish_is_noop_when_unconnected() -> None:
    # No servers / before connect → publish must not raise (snapshot-only degrade).
    pub = NatsSessionPublisher(servers=None)
    pub.publish("alice", {"session": "t1", "running": True})  # does not raise


def test_publish_schedules_event_to_the_connection() -> None:
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    received: list[tuple[str, bytes]] = []
    done = threading.Event()

    class _NC:
        async def publish(self, subject: str, data: bytes) -> None:
            received.append((subject, data))
            done.set()

    pub = NatsSessionPublisher(servers="nats://x")
    pub._nc = _NC()  # noqa: SLF001 — simulate a live connection
    pub._loop = loop  # noqa: SLF001

    pub.publish("alice", {"session": "t1", "running": True})

    assert done.wait(2.0)  # the producer-thread publish reached the loop's connection
    loop.call_soon_threadsafe(loop.stop)
    assert received == [
        ("session.alice.status", json.dumps({"session": "t1", "running": True}).encode())
    ]


def test_publish_fail_soft_on_broken_connection() -> None:
    # A connection without a usable publish (or a closed loop) must be swallowed.
    pub = NatsSessionPublisher(servers="nats://x")
    pub._nc = object()  # noqa: SLF001 — no .publish → AttributeError inside publish()
    pub._loop = asyncio.new_event_loop()  # noqa: SLF001
    pub.publish("alice", {"x": 1})  # does not raise


async def test_connect_is_noop_when_servers_unset() -> None:
    pub = NatsSessionPublisher(servers=None)
    await pub.connect(asyncio.get_running_loop())
    pub.publish("alice", {"x": 1})  # still a no-op, no connection opened


async def test_connect_opens_connection_via_nats(monkeypatch) -> None:
    opened: dict[str, object] = {}

    class _NC:
        async def publish(self, subject: str, data: bytes) -> None: ...

    async def _fake_connect(servers, **opts):  # noqa: ANN001
        opened["servers"] = servers
        opened["opts"] = opts
        return _NC()

    monkeypatch.setattr("nats.connect", _fake_connect)
    pub = NatsSessionPublisher(servers="nats://a,nats://b", token="tok")

    await pub.connect(asyncio.get_running_loop())

    assert opened["servers"] == ["nats://a", "nats://b"]  # comma-split
    assert opened["opts"]["token"] == "tok"


async def test_connect_fail_soft_does_not_abort(monkeypatch) -> None:
    async def _boom(servers, **opts):  # noqa: ANN001
        raise ConnectionError("nats down")

    monkeypatch.setattr("nats.connect", _boom)
    pub = NatsSessionPublisher(servers="nats://x")

    await pub.connect(asyncio.get_running_loop())  # swallowed
    pub.publish("alice", {"x": 1})  # degraded to no-op, no raise
