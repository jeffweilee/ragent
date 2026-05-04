"""T0.10a — broker topology: standalone vs sentinel dispatch, missing sentinel var exits."""

import pytest
from taskiq_redis import ListQueueBroker, ListQueueSentinelBroker

from ragent.bootstrap.broker import _make_broker


def test_standalone_returns_list_queue_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_MODE", "standalone")
    monkeypatch.setenv("REDIS_BROKER_URL", "redis://testhost:6379/0")
    assert isinstance(_make_broker(), ListQueueBroker)


def test_standalone_uses_broker_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_MODE", "standalone")
    monkeypatch.setenv("REDIS_BROKER_URL", "redis://myhost:6380/2")
    broker = _make_broker()
    kwargs = broker.connection_pool.connection_kwargs
    assert kwargs["host"] == "myhost"
    assert kwargs["port"] == 6380
    assert kwargs["db"] == 2


def test_default_mode_is_standalone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REDIS_MODE", raising=False)
    monkeypatch.setenv("REDIS_BROKER_URL", "redis://localhost:6379/0")
    assert isinstance(_make_broker(), ListQueueBroker)


def test_sentinel_returns_sentinel_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_MODE", "sentinel")
    monkeypatch.setenv("REDIS_SENTINEL_HOSTS", "s1:26379,s2:26380")
    monkeypatch.setenv("REDIS_BROKER_SENTINEL_MASTER", "my-master")
    assert isinstance(_make_broker(), ListQueueSentinelBroker)


def test_sentinel_parses_hosts_and_master(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_MODE", "sentinel")
    monkeypatch.setenv("REDIS_SENTINEL_HOSTS", "host1:26379,host2:26380")
    monkeypatch.setenv("REDIS_BROKER_SENTINEL_MASTER", "ragent-broker")
    broker = _make_broker()
    assert isinstance(broker, ListQueueSentinelBroker)
    assert broker.master_name == "ragent-broker"
    # sentinel.sentinels is a list of Redis client objects; verify hosts via connection pool
    sentinel_hosts = {
        (r.connection_pool.connection_kwargs["host"], r.connection_pool.connection_kwargs["port"])
        for r in broker.sentinel.sentinels
    }
    assert ("host1", 26379) in sentinel_hosts
    assert ("host2", 26380) in sentinel_hosts


def test_sentinel_missing_hosts_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_MODE", "sentinel")
    monkeypatch.delenv("REDIS_SENTINEL_HOSTS", raising=False)
    with pytest.raises(SystemExit):
        _make_broker()
