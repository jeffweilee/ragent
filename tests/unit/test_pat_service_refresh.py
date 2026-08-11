"""T-PAT.9 — PatService refresh state machine (401/400/429, lock, rotation)."""

from __future__ import annotations

import fakeredis
import pytest
import redis as redis_lib
from structlog.testing import capture_logs

from ragent.clients.pat_cache import PatCache
from ragent.clients.pat_refresh_client import (
    PatRefreshBadRequest,
    PatRefreshRateLimited,
    PatRefreshUnauthorized,
)
from ragent.clients.redis_guard import RedisCircuit
from ragent.errors.codes import HttpErrorCode
from ragent.services.pat_service import (
    PatInternalError,
    PatReauthRequired,
    PatRefreshExhausted,
)
from tests.unit.pat_fakes import FakeInitClient, FakeRefreshClient, build_service, sign


def _seed_expired(repo, cache, cipher, nt="alice"):
    """An expired-but-active PAT in both DB and redis → resolve() will refresh."""
    expired = cipher.encrypt(sign(nt, exp_delta=-10))
    repo.rows[nt] = {"user_id": nt, "pat_cipher": expired, "status": "active"}
    cache.put(nt, expired)


async def test_success_rotates_db_and_cache() -> None:
    new_pat = sign("alice")
    service, repo, cache, cipher = build_service(refresh_client=FakeRefreshClient([new_pat]))
    _seed_expired(repo, cache, cipher)

    assert await service.resolve("alice") == new_pat
    assert cipher.decrypt(repo.rows["alice"]["pat_cipher"]) == new_pat
    assert cipher.decrypt(cache.get("alice")) == new_pat


async def test_401_invalidates_and_clears() -> None:
    rc = FakeRefreshClient([PatRefreshUnauthorized()])
    service, repo, cache, cipher = build_service(refresh_client=rc)
    _seed_expired(repo, cache, cipher)

    with pytest.raises(PatReauthRequired):
        await service.resolve("alice")

    assert repo.rows["alice"]["status"] == "invalid"
    assert cache.get("alice") is None
    assert repo.mark_invalid_calls == ["alice"]


async def test_429_is_not_retried() -> None:
    """T-RETRY.3 — a rate-limited refresh fails this request instead of
    retrying. The old budget (30 s × 4 + backoff ≈ 123 s) sat on the
    `/brainagent/v1` request path, where the caller is fail-open: it would give
    up on the PAT anyway, just two minutes later and after four calls to a
    service that was already saying "slow down"."""
    new_pat = sign("alice")
    rc = FakeRefreshClient([PatRefreshRateLimited(), new_pat])
    service, repo, cache, cipher = build_service(refresh_client=rc)
    _seed_expired(repo, cache, cipher)

    with pytest.raises(PatRefreshExhausted):
        await service.resolve("alice")
    assert len(rc.calls) == 1


async def test_429_rejects_but_keeps_active() -> None:
    rc = FakeRefreshClient([PatRefreshRateLimited()] * 10)
    service, repo, cache, cipher = build_service(refresh_client=rc)
    _seed_expired(repo, cache, cipher)

    with pytest.raises(PatRefreshExhausted):
        await service.resolve("alice")

    # rate-limit must NEVER invalidate the user's authorization.
    assert repo.rows["alice"]["status"] == "active"
    assert repo.mark_invalid_calls == []
    assert len(rc.calls) == 1


async def test_400_is_internal_error_and_leaves_state() -> None:
    rc = FakeRefreshClient([PatRefreshBadRequest()])
    service, repo, cache, cipher = build_service(refresh_client=rc)
    _seed_expired(repo, cache, cipher)

    with pytest.raises(PatInternalError):
        await service.resolve("alice")

    assert repo.rows["alice"]["status"] == "active"


async def test_revoked_mid_refresh_is_not_resurrected() -> None:
    # T-PAT.24: the row is deleted (revoked) while the refresh HTTP call is in
    # flight. The rotation must NOT recreate the authorization, must not publish
    # the new token to redis, and must send the caller back through authorize.
    new_pat = sign("alice")
    rc = FakeRefreshClient([new_pat])
    service, repo, cache, cipher = build_service(refresh_client=rc)
    _seed_expired(repo, cache, cipher)
    current = cipher.decrypt(repo.rows["alice"]["pat_cipher"])
    del repo.rows["alice"]  # revoke lands while the refresh is in flight
    cache.evict("alice")

    with capture_logs() as captured, pytest.raises(PatReauthRequired):
        await service._do_refresh("alice", current)

    assert "alice" not in repo.rows  # no INSERT — the revoke stands
    assert cache.get("alice") is None  # the rotated token never reaches redis
    revoked = [e for e in captured if e.get("event") == "pat.refresh.revoked"]
    assert len(revoked) == 1
    assert revoked[0]["user_id"] == "alice"
    assert revoked[0]["error_code"] == HttpErrorCode.PAT_REAUTH_REQUIRED


@pytest.mark.parametrize(
    "rotation,why",
    [
        ("not-a-jwt", "malformed"),
        # Never trust the refresh service to name the owner — the same binding
        # check authorize applies to a freshly minted PAT.
        (sign("mallory"), "bound to another nt"),
    ],
)
async def test_unverifiable_rotation_is_not_persisted_and_invalidates(
    rotation: str, why: str
) -> None:
    """`PatRefreshClient` only checks the envelope carries a non-empty
    `patToken`, so a malformed rotation would otherwise be encrypted and stored.

    The rejected token is never persisted — but the row IS marked invalid and
    the cache evicted (PR #243 Codex P1). Raising `PatReauthRequired` while
    leaving the row `active` would make `status` claim a healthy authorization
    that `resolve` deterministically fails on: the stored PAT is already
    expired, so every retry asks the same broken upstream again."""
    rc = FakeRefreshClient([rotation])
    service, repo, cache, cipher = build_service(refresh_client=rc)
    _seed_expired(repo, cache, cipher)
    before = repo.rows["alice"]["pat_cipher"]

    with pytest.raises(PatReauthRequired):
        await service.resolve("alice")

    assert repo.rows["alice"]["pat_cipher"] == before, why  # rejection not stored
    assert repo.rows["alice"]["status"] == "invalid", why
    assert cache.get("alice") is None, why


async def test_rejected_rotation_makes_status_agree_with_resolve() -> None:
    """The invariant this protects, end to end: after a rejected rotation the
    status endpoint must not still say `active`."""
    service, repo, cache, cipher = build_service(refresh_client=FakeRefreshClient(["not-a-jwt"]))
    _seed_expired(repo, cache, cipher)

    with pytest.raises(PatReauthRequired):
        await service.resolve("alice")

    assert (await service.status(nt="alice")).status == "invalid"


async def test_refresh_lock_loser_uses_freshly_cached_token() -> None:
    # Simulate a concurrent refresh already holding the lock and having written a
    # fresh token: the loser must return it WITHOUT calling the refresh client.
    rc = FakeRefreshClient([])  # empty — a call would IndexError
    service, repo, cache, cipher = build_service(refresh_client=rc)

    fresh = sign("alice")
    # expired token in DB (drives resolve into _refresh), fresh valid token in cache
    repo.rows["alice"] = {
        "user_id": "alice",
        "pat_cipher": cipher.encrypt(sign("alice", exp_delta=-10)),
        "status": "active",
    }
    cache.acquire_refresh_lock("alice")  # someone else holds it
    cache.put("alice", cipher.encrypt(fresh))

    # First resolve sees the expired cache entry? No — cache now holds `fresh`
    # (valid), so resolve returns it directly. Drive the lock path explicitly:
    assert await service._refresh("alice", sign("alice", exp_delta=-10)) == fresh
    assert rc.calls == []  # loser never called the refresh service


async def test_refresh_loser_polls_until_winner_publishes_token() -> None:
    # Lock is held by another holder; the cache is initially stale, then the
    # "winner" publishes a fresh token during our first poll-sleep. The loser
    # must return it WITHOUT stampeding the refresh service.
    rc = FakeRefreshClient([])  # a call would IndexError
    fresh = sign("alice")

    published: dict = {}

    async def sleeper(_seconds: float) -> None:
        # Simulate the winner finishing mid-poll: publish the rotated token once.
        if not published:
            published["done"] = True
            cache.put("alice", cipher.encrypt(fresh))

    service, repo, cache, cipher = build_service(refresh_client=rc)
    # rebuild service with our side-effecting sleeper + the same collaborators
    from ragent.services.pat_service import PatService

    service = PatService(
        verifier=service._verifier,
        cipher=cipher,
        repo=repo,
        cache=cache,
        refresh_client=rc,
        init_client=FakeInitClient(sign()),
        sleeper=sleeper,
    )
    cache.acquire_refresh_lock("alice")  # another holder owns the lock
    repo.rows["alice"] = {
        "user_id": "alice",
        "pat_cipher": cipher.encrypt(sign("alice", exp_delta=-10)),
        "status": "active",
    }

    assert await service._refresh("alice", sign("alice", exp_delta=-10)) == fresh
    assert rc.calls == []


# --- Redis unavailable: do not poll for a lock nobody can take (T-RG.4) ---


def _dead_cache() -> PatCache:
    """A PatCache whose Redis refuses every command."""

    class _DeadRedis:
        def __getattr__(self, _name):
            def _fail(*_a, **_kw):
                raise redis_lib.ConnectionError("connection refused")

            return _fail

    return PatCache(
        _DeadRedis(),
        ttl_seconds=41400,
        lock_ttl_seconds=10,
        tombstone_ttl_seconds=123,
        circuit=RedisCircuit("test", failure_threshold=1, cooldown_seconds=300.0),
    )


def _service_with(cache: PatCache, refresh_client, sleeps: list[float]):
    from ragent.services.pat_service import PatService

    base, repo, _, cipher = build_service()

    async def _record(seconds: float) -> None:
        sleeps.append(seconds)

    return PatService(
        verifier=base._verifier,
        cipher=cipher,
        repo=repo,
        cache=cache,
        refresh_client=refresh_client,
        init_client=FakeInitClient(sign()),
        sleeper=_record,
    )


async def test_refresh_does_not_poll_when_redis_is_unavailable() -> None:
    """The poll loop exists to let a LOSER wait for the winner's rotation.

    With Redis down there is no winner and never will be: `acquire_refresh_lock`
    cannot write, so every attempt fails. Polling then burns the full sleep
    budget (10 x 0.5s by default) plus a blocking Redis call per attempt, on
    every request — the exact "everything got slow" symptom. Proceed
    unserialised instead: still correct, just no herd protection while Redis is
    down.
    """
    fresh = sign("alice")
    sleeps: list[float] = []
    rc = FakeRefreshClient([fresh])
    service = _service_with(_dead_cache(), rc, sleeps)
    service._repo.rows["alice"] = {
        "user_id": "alice",
        "pat_cipher": service._cipher.encrypt(sign("alice", exp_delta=-10)),
        "status": "active",
    }

    assert await service._refresh("alice", sign("alice", exp_delta=-10)) == fresh
    assert sleeps == []  # never slept
    assert rc.calls  # went straight to the refresh service


async def test_refresh_still_polls_when_the_lock_is_genuinely_held() -> None:
    """The fast path must not regress herd protection on a HEALTHY Redis."""
    fresh = sign("alice")
    sleeps: list[float] = []
    cache = PatCache(
        fakeredis.FakeStrictRedis(decode_responses=True),
        ttl_seconds=41400,
        lock_ttl_seconds=10,
        tombstone_ttl_seconds=123,
    )
    rc = FakeRefreshClient([])  # a call would IndexError — the loser must not call
    service = _service_with(cache, rc, sleeps)
    cache.acquire_refresh_lock("alice")  # another holder owns it

    cipher = service._cipher
    service._repo.rows["alice"] = {
        "user_id": "alice",
        "pat_cipher": cipher.encrypt(sign("alice", exp_delta=-10)),
        "status": "active",
    }

    async def _publish(seconds: float) -> None:
        sleeps.append(seconds)
        cache.put("alice", cipher.encrypt(fresh))

    service._sleeper = _publish
    assert await service._refresh("alice", sign("alice", exp_delta=-10)) == fresh
    assert sleeps  # it did wait for the winner
    assert rc.calls == []
