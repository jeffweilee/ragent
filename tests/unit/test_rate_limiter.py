"""T3.13 — RateLimiter: fixed-window per-key INCR+EXPIRE counter (B31)."""

import time

import fakeredis
import pytest


def _make_limiter(fake_redis=None):
    from ragent.clients.rate_limiter import RateLimiter

    r = fake_redis or fakeredis.FakeRedis()
    return RateLimiter(redis_client=r)


# --- constructor / topology ---


def test_standalone_ctor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_MODE", "standalone")
    monkeypatch.setenv("REDIS_RATELIMIT_URL", "redis://localhost:6379/1")
    from ragent.clients.rate_limiter import RateLimiter

    limiter = RateLimiter.from_env()
    assert limiter is not None


def test_sentinel_ctor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_MODE", "sentinel")
    monkeypatch.setenv("REDIS_SENTINEL_HOSTS", "s1:26379")
    monkeypatch.setenv("REDIS_RATELIMIT_SENTINEL_MASTER", "ratelimit-master")
    from ragent.clients.rate_limiter import RateLimiter

    limiter = RateLimiter.from_env()
    assert limiter is not None


# --- behavioral: fixed-window counter ---


def test_under_limit_is_allowed():
    limiter = _make_limiter()
    result = limiter.check("user:alice", limit=5, window_seconds=60)
    assert result.allowed is True
    assert result.remaining == 4


def test_remaining_decrements_per_call():
    limiter = _make_limiter()
    r1 = limiter.check("user:bob", limit=3, window_seconds=60)
    r2 = limiter.check("user:bob", limit=3, window_seconds=60)
    assert r1.remaining == 2
    assert r2.remaining == 1


def test_at_limit_is_blocked():
    limiter = _make_limiter()
    for _ in range(3):
        limiter.check("user:carol", limit=3, window_seconds=60)
    result = limiter.check("user:carol", limit=3, window_seconds=60)
    assert result.allowed is False
    assert result.remaining == 0


def test_blocked_result_has_reset_at():
    limiter = _make_limiter()
    for _ in range(2):
        limiter.check("user:dave", limit=2, window_seconds=60)
    before = time.time()
    result = limiter.check("user:dave", limit=2, window_seconds=60)
    assert result.allowed is False
    assert result.reset_at is not None
    assert result.reset_at >= before


def test_different_keys_are_isolated():
    limiter = _make_limiter()
    for _ in range(2):
        limiter.check("user:eve", limit=2, window_seconds=60)
    blocked = limiter.check("user:eve", limit=2, window_seconds=60)
    fresh = limiter.check("user:frank", limit=2, window_seconds=60)
    assert blocked.allowed is False
    assert fresh.allowed is True


def test_key_prefix_applied():
    fake = fakeredis.FakeRedis()
    limiter = _make_limiter(fake_redis=fake)
    limiter.check("mykey", limit=10, window_seconds=60)
    keys = [k.decode() for k in fake.keys("*")]
    assert any(k.startswith("ratelimit:") for k in keys)


def test_window_expiry_resets_counter():
    fake = fakeredis.FakeRedis()
    limiter = _make_limiter(fake_redis=fake)
    for _ in range(3):
        limiter.check("user:grace", limit=3, window_seconds=1)
    blocked = limiter.check("user:grace", limit=3, window_seconds=1)
    assert blocked.allowed is False

    # Manually expire the key to simulate window reset
    fake.delete("ratelimit:user:grace")
    result = limiter.check("user:grace", limit=3, window_seconds=1)
    assert result.allowed is True
