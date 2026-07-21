"""T-PAT.7 — PatService.authorize: verify + bind + encrypt + persist."""

from __future__ import annotations

import pytest

from ragent.auth.pat_jwt import PatTokenInvalid
from tests.unit.pat_fakes import build_service, sign


async def test_authorize_writes_active_row_and_caches() -> None:
    service, repo, cache, cipher = build_service()
    pat = sign("alice")

    await service.authorize(nt="alice", pat_token=pat)

    assert repo.rows["alice"]["status"] == "active"
    # DB + redis both hold the *encrypted* PAT, decrypting back to what we stored.
    assert cipher.decrypt(repo.rows["alice"]["pat_cipher"]) == pat
    cached = cache.get("alice")
    assert cached is not None and cipher.decrypt(cached) == pat


async def test_authorize_rejects_bad_token_and_writes_nothing() -> None:
    service, repo, cache, _ = build_service()

    with pytest.raises(PatTokenInvalid):
        await service.authorize(nt="alice", pat_token="not.a.jwt")

    assert repo.rows == {}
    assert cache.get("alice") is None


async def test_authorize_rejects_nt_binding_mismatch() -> None:
    service, repo, cache, _ = build_service()

    # A valid PAT, but bound to bob — alice must not be able to store it.
    with pytest.raises(PatTokenInvalid):
        await service.authorize(nt="alice", pat_token=sign("bob"))

    assert repo.rows == {}
    assert cache.get("alice") is None
