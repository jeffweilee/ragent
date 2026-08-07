"""T-PAT.3 — PatTokenVerifier: static-PEM PAT-JWT verification + nt binding."""

from __future__ import annotations

import time

import pytest
from joserfc import jwt as _jwt
from joserfc.jwk import RSAKey

from ragent.auth.pat_jwt import PatTokenInvalid, PatTokenVerifier, import_pat_public_key
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


def test_missing_exp_is_rejected() -> None:
    # A PAT with no exp must NOT verify — otherwise it would never be refreshed.
    no_exp = _jwt.encode({"alg": "RS256"}, {"iss": _ISS, "aud": _AUD, "nt": "alice"}, _KEY)
    with pytest.raises(PatTokenInvalid):
        _verifier().verify(no_exp)


def _verifier_from(public_key_value: str) -> PatTokenVerifier:
    return PatTokenVerifier(
        key=import_pat_public_key(public_key_value, "RS256"),
        algorithm="RS256",
        expected_iss=_ISS,
        expected_aud=_AUD,
        nt_claim=_NT_CLAIM,
    )


def test_import_accepts_full_pem() -> None:
    pem = _KEY.as_pem(private=False).decode()
    assert _verifier_from(pem).verify(_sign())["nt"] == "alice"


def test_import_accepts_headerless_one_line_base64_der() -> None:
    # What SSO often hands out: the base64 body of the SPKI PEM, no headers/newlines.
    pem = _KEY.as_pem(private=False).decode()
    one_line = "".join(ln for ln in pem.splitlines() if "-----" not in ln)
    assert _verifier_from(one_line).verify(_sign())["nt"] == "alice"


def test_import_unescapes_literal_backslash_n_in_pem() -> None:
    pem_escaped = _KEY.as_pem(private=False).decode().replace("\n", "\\n")
    assert _verifier_from(pem_escaped).verify(_sign())["nt"] == "alice"


def test_import_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="neither a PEM nor a base64 DER"):
        import_pat_public_key("not a key!!!", "RS256")


# --- ignore_expiry (T-PAT.27) --------------------------------------------


def test_ignore_expiry_accepts_an_expired_but_otherwise_valid_token() -> None:
    verifier = _verifier()
    token = _sign(exp=int(time.time()) - 3600)

    with pytest.raises(PatTokenInvalid):
        verifier.verify(token)
    assert verifier.verify(token, ignore_expiry=True)["nt"] == "alice"


def test_ignore_expiry_still_enforces_signature_issuer_and_audience() -> None:
    # It relaxes time, nothing else — otherwise `status` would report a
    # structurally broken PAT as usable.
    verifier = _verifier()
    for bad in (
        _sign(exp=int(time.time()) - 10, iss="https://evil.example"),
        _sign(exp=int(time.time()) - 10, aud="someone-else"),
    ):
        with pytest.raises(PatTokenInvalid):
            verifier.verify(bad, ignore_expiry=True)


def test_ignore_expiry_still_requires_the_exp_claim() -> None:
    # A PAT with no `exp` must never verify: resolve() would then hold it
    # forever and never rotate it.
    no_exp = _jwt.encode({"alg": "RS256"}, {"iss": _ISS, "aud": _AUD, "nt": "alice"}, _KEY)
    with pytest.raises(PatTokenInvalid):
        _verifier().verify(no_exp, ignore_expiry=True)


def test_ignore_expiry_tolerates_iat_and_nbf() -> None:
    # Regression guard: pinning `now` to 0 instead of widening the leeway would
    # make these two claims look like the future and fail.
    now = int(time.time())
    token = _sign(exp=now - 10, iat=now, nbf=now - 60)
    assert _verifier().verify(token, ignore_expiry=True)["nt"] == "alice"
