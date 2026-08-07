"""T-PAT.27 — PatService.status: a zero-side-effect read of authorization state.

Two properties matter more than the mapping itself:

* **No side effects.** Routing this through `resolve()` would make a per-page-load
  GET trigger refresh round-trips, DB writes and possible `mark_invalid`. The
  "never called" assertions here are the guard against someone later noticing the
  mappings look similar and folding the two together.
* **The invariant is one-directional.** `status` must never say `active` when
  `resolve` would fail; the reverse is intended and is how the post-`expireDate`
  window reaches the user.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from ragent.utility.datetime import utcnow
from tests.unit.pat_fakes import FakeRefreshClient, build_service, sign

_FAR_FUTURE = date(2099, 1, 1)


def _row(cipher, *, nt="alice", status="active", exp_delta=3600, expires=_FAR_FUTURE, token=None):
    return {
        "user_id": nt,
        "pat_cipher": cipher.encrypt(token if token is not None else sign(nt, exp_delta=exp_delta)),
        "status": status,
        "authorized_at": utcnow(),
        "authorization_expires_at": expires,
    }


async def test_no_row_is_none() -> None:
    service, _, _, _ = build_service()
    assert (await service.status(nt="alice")).status == "none"


async def test_active_and_usable_is_active() -> None:
    service, repo, _, cipher = build_service()
    repo.rows["alice"] = _row(cipher)
    assert (await service.status(nt="alice")).status == "active"


async def test_invalid_row_is_invalid() -> None:
    service, repo, _, cipher = build_service()
    repo.rows["alice"] = _row(cipher, status="invalid")
    assert (await service.status(nt="alice")).status == "invalid"


async def test_undecryptable_ciphertext_is_invalid() -> None:
    # NOT a column read: no code path ever flips `status` to invalid on a
    # decryption failure, so reading the column alone would report a healthy
    # authorization that fails on every request.
    service, repo, _, _ = build_service()
    repo.rows["alice"] = {
        "user_id": "alice",
        "pat_cipher": "v1.garbage.garbage",
        "status": "active",
        "authorized_at": utcnow(),
        "authorization_expires_at": _FAR_FUTURE,
    }
    assert (await service.status(nt="alice")).status == "invalid"


@pytest.mark.parametrize(
    "token,why",
    [
        ("not-a-jwt", "malformed"),
        (sign("mallory"), "bound to another nt"),
    ],
)
async def test_decryptable_but_unusable_token_is_invalid(token: str, why: str) -> None:
    service, repo, _, cipher = build_service()
    repo.rows["alice"] = _row(cipher, token=token)
    assert (await service.status(nt="alice")).status == "invalid", why


async def test_locally_expired_pat_still_reports_active() -> None:
    """The single most important case. An expired PAT is refreshable and is the
    normal steady state — rotated roughly twice a day. Reporting it as broken
    would prompt a healthy account to re-authorize daily."""
    service, repo, _, cipher = build_service()
    repo.rows["alice"] = _row(cipher, exp_delta=-10)
    assert (await service.status(nt="alice")).status == "active"


async def test_past_the_authorization_window_is_invalid_even_while_the_row_is_active() -> None:
    # The one-directional invariant in action: `resolve` could still hand out
    # this token, but the upstream has already dropped the authorization.
    service, repo, _, cipher = build_service()
    repo.rows["alice"] = _row(cipher, expires=utcnow().date() - timedelta(days=1))

    assert (await service.status(nt="alice")).status == "invalid"
    assert repo.rows["alice"]["status"] == "active"  # the row is NOT mutated


async def test_null_window_is_ignored() -> None:
    # Rows written before migration 018: unknown, not "expired".
    service, repo, _, cipher = build_service()
    repo.rows["alice"] = _row(cipher, expires=None)
    assert (await service.status(nt="alice")).status == "active"


async def test_status_has_no_side_effects() -> None:
    """Never refresh, never touch redis, never write. The regression guard
    against re-plumbing status onto `resolve()`."""
    rc = FakeRefreshClient([])  # any call would IndexError
    service, repo, cache, cipher = build_service(refresh_client=rc)
    repo.rows["alice"] = _row(cipher, exp_delta=-10)  # expired → resolve WOULD refresh
    before = repo.rows["alice"]["pat_cipher"]

    await service.status(nt="alice")

    assert rc.calls == []  # no refresh HTTP call
    assert cache.get("alice") is None  # no cache re-fill
    assert repo.rows["alice"]["pat_cipher"] == before  # no DB write
    assert repo.mark_invalid_calls == []


async def test_timestamps_are_serialised_for_the_wire() -> None:
    service, repo, _, cipher = build_service()
    repo.rows["alice"] = _row(cipher, expires=date(2027, 7, 29))

    result = await service.status(nt="alice")

    assert result.authorization_expires_at == "2027-07-29"
    assert result.authorized_at is not None and result.authorized_at.endswith("Z")


async def test_absent_row_carries_no_timestamps() -> None:
    service, _, _, _ = build_service()
    result = await service.status(nt="ghost")
    assert result.authorized_at is None
    assert result.authorization_expires_at is None
