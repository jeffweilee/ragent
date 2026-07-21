"""T-PAT.3 — PatTokenVerifier: static-PEM PAT-JWT verification + nt binding."""

from __future__ import annotations

import time

import pytest
from joserfc import jwt as _jwt
from joserfc.jwk import RSAKey

from ragent.auth.pat_jwt import PatTokenInvalid, PatTokenVerifier
from ragent.errors.codes import HttpErrorCode

_ISS = "https://sso.example/pat"
_AUD = "ragent-agent"
_NT_CLAIM = "nt"

_KEY = RSAKey.generate_key(2048)
_OTHER_KEY = RSAKey.generate_key(2048)


def _verifier() -> PatTokenVerifier:
    return PatTokenVerifier(
        key=RSAKey.import_key(_KEY.as_pem(private=False)),
        algorithm="RS256",
        expected_iss=_ISS,
        expected_aud=_AUD,
        nt_claim=_NT_CLAIM,
    )


def _sign(key: RSAKey = _KEY, **overrides) -> str:
    now = int(time.time())
    claims = {"iss": _ISS, "aud": _AUD, "exp": now + 3600, "nt": "alice", **overrides}
    return _jwt.encode({"alg": "RS256"}, claims, key)


def test_valid_token_returns_claims_and_nt() -> None:
    verifier = _verifier()

    claims = verifier.verify(_sign())

    assert claims["nt"] == "alice"
    assert verifier.nt_of(claims) == "alice"


def test_pat_token_invalid_carries_reauth_code() -> None:
    with pytest.raises(PatTokenInvalid) as exc:
        _verifier().verify("")
    assert exc.value.error_code == HttpErrorCode.PAT_REAUTH_REQUIRED
    assert exc.value.http_status == 401


@pytest.mark.parametrize(
    "overrides",
    [
        {"iss": "https://evil.example"},
        {"aud": "someone-else"},
        {"exp": int(time.time()) - 10},
    ],
)
def test_bad_claims_raise(overrides: dict) -> None:
    with pytest.raises(PatTokenInvalid):
        _verifier().verify(_sign(**overrides))


def test_wrong_signature_raises() -> None:
    forged = _sign(key=_OTHER_KEY)
    with pytest.raises(PatTokenInvalid):
        _verifier().verify(forged)


def test_malformed_token_raises() -> None:
    with pytest.raises(PatTokenInvalid):
        _verifier().verify("not.a.jwt")


def test_missing_nt_claim_raises() -> None:
    verifier = _verifier()
    claims = verifier.verify(_sign(nt=""))
    with pytest.raises(PatTokenInvalid):
        verifier.nt_of(claims)
