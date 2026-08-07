"""T-PAT.9 — PatService refresh state machine (401/400/429, lock, rotation)."""

from __future__ import annotations

import pytest
from structlog.testing import capture_logs

from ragent.clients.pat_refresh_client import (
    PatRefreshBadRequest,
    PatRefreshRateLimited,
    PatRefreshUnauthorized,
)
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


async def test_429_then_success_within_budget() -> None:
    new_pat = sign("alice")
    rc = FakeRefreshClient([PatRefreshRateLimited(), PatRefreshRateLimited(), new_pat])
    service, repo, cache, cipher = build_service(refresh_client=rc, max_retries=3)
    _seed_expired(repo, cache, cipher)

    assert await service.resolve("alice") == new_pat
    assert len(rc.calls) == 3  # two 429s then success


async def test_429_exhausted_rejects_but_keeps_active() -> None:
    rc = FakeRefreshClient([PatRefreshRateLimited()] * 10)
    service, repo, cache, cipher = build_service(refresh_client=rc, max_retries=3)
    _seed_expired(repo, cache, cipher)

    with pytest.raises(PatRefreshExhausted):
        await service.resolve("alice")

    # rate-limit must NEVER invalidate the user's authorization.
    assert repo.rows["alice"]["status"] == "active"
    assert repo.mark_invalid_calls == []
    assert len(rc.calls) == 4  # initial + 3 retries


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


async def test_unverifiable_rotation_is_not_persisted() -> None:
    # T-PAT.24: `PatRefreshClient` only checks the envelope carries a non-empty
    # `patToken`, so a malformed rotation would otherwise be encrypted and stored
    # — leaving an `active` row holding a PAT nothing can use.
    rc = FakeRefreshClient(["not-a-jwt"])
    service, repo, cache, cipher = build_service(refresh_client=rc)
    _seed_expired(repo, cache, cipher)
    before = repo.rows["alice"]["pat_cipher"]

    with pytest.raises(PatReauthRequired):
        await service.resolve("alice")

    assert repo.rows["alice"]["pat_cipher"] == before  # untouched
    assert cipher.decrypt(cache.get("alice")) == cipher.decrypt(before)


async def test_rotation_bound_to_another_nt_is_rejected() -> None:
    # Never trust the refresh service to name the owner — same binding check
    # authorize applies to a freshly minted PAT.
    rc = FakeRefreshClient([sign("mallory")])
    service, repo, cache, cipher = build_service(refresh_client=rc)
    _seed_expired(repo, cache, cipher)
    before = repo.rows["alice"]["pat_cipher"]

    with pytest.raises(PatReauthRequired):
        await service.resolve("alice")

    assert repo.rows["alice"]["pat_cipher"] == before
    assert repo.rows["alice"]["status"] == "active"  # nothing written on failure


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
