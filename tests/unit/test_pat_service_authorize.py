"""T-PAT.17 — PatService.authorize: init-mint + verify + bind + encrypt + persist.

The FE no longer submits a `{patToken}`; ragent mints one via the init service
(on-behalf-of the inbound SSO id token) and then verifies + binds + stores it.
Init HTTP failures surface as the service's typed exceptions.
"""

from __future__ import annotations

import pytest
from structlog.testing import capture_logs

from ragent.auth.pat_jwt import PatTokenInvalid
from ragent.clients.pat_init_client import (
    PatInitBadRequest,
    PatInitRateLimited,
    PatInitTransient,
    PatInitUnauthorized,
)
from ragent.services.pat_service import (
    PatInitThrottled,
    PatInitUnavailable,
    PatInternalError,
    PatReauthRequired,
)
from tests.unit.pat_fakes import FakeInitClient, build_service, sign


async def test_authorize_mints_via_init_and_writes_active_row() -> None:
    minted = sign("alice")
    init = FakeInitClient(minted)
    service, repo, cache, cipher = build_service(init_client=init)

    await service.authorize(nt="alice", id_token="ID-TOKEN")

    assert init.calls == ["ID-TOKEN"]  # the inbound SSO id token was forwarded
    assert repo.rows["alice"]["status"] == "active"
    assert cipher.decrypt(repo.rows["alice"]["pat_cipher"]) == minted
    cached = cache.get("alice")
    assert cached is not None and cipher.decrypt(cached) == minted


async def test_authorize_rejects_nt_binding_mismatch() -> None:
    # Init hands back a valid PAT bound to bob — alice must not be able to store it.
    service, repo, cache, _ = build_service(init_client=FakeInitClient(sign("bob")))

    with pytest.raises(PatTokenInvalid):
        await service.authorize(nt="alice", id_token="ID-TOKEN")

    assert repo.rows == {}
    assert cache.get("alice") is None


@pytest.mark.parametrize(
    "minted,reason",
    [("not.a.jwt", "minted_token_invalid"), (None, "nt_mismatch")],
)
async def test_authorize_logs_a_terminal_failure_event(minted: str | None, reason: str) -> None:
    """`pat.authorize.started` must never dangle without an outcome — a PAT the
    init service minted badly (or for the wrong nt) logs `pat.authorize.failed`
    carrying the error_code (00_rule.md §Service Boundary Logs)."""
    token = minted if minted is not None else sign("bob")
    service, _, _, _ = build_service(init_client=FakeInitClient(token))

    with capture_logs() as captured, pytest.raises(PatTokenInvalid):
        await service.authorize(nt="alice", id_token="ID-TOKEN")

    failures = [e for e in captured if e.get("event") == "pat.authorize.failed"]
    assert len(failures) == 1
    assert failures[0]["user_id"] == "alice"
    assert failures[0]["error_code"] == "PAT_REAUTH_REQUIRED"
    assert failures[0]["reason"] == reason


@pytest.mark.parametrize(
    "init_exc,expected",
    [
        (PatInitUnauthorized(), PatReauthRequired),
        (PatInitBadRequest(), PatInternalError),
        (PatInitRateLimited(), PatInitThrottled),
        (PatInitTransient("boom"), PatInitUnavailable),
    ],
)
async def test_authorize_maps_init_errors_and_writes_nothing(
    init_exc: Exception, expected: type[Exception]
) -> None:
    service, repo, cache, _ = build_service(init_client=FakeInitClient(init_exc))

    with pytest.raises(expected):
        await service.authorize(nt="alice", id_token="ID-TOKEN")

    assert repo.rows == {}
    assert cache.get("alice") is None
