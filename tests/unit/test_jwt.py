"""T8.1 — decode-only JWT validation (§3.5 / §3.5.1, accepted-risk: no signature verify)."""

from __future__ import annotations

import base64
import json

import pytest

from ragent.auth.jwt import JwtAuthError, decode_jwt_payload


def _b64url(payload: bytes) -> str:
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _make_token(payload: dict, *, header: dict | None = None, signature: str = "sig") -> str:
    """Build a JWT-shaped string. Signature segment is opaque (decode-only)."""
    header_b64 = _b64url(json.dumps(header or {"alg": "none"}).encode())
    payload_b64 = _b64url(json.dumps(payload).encode())
    return f"{header_b64}.{payload_b64}.{signature}"


_NOW = 1_700_000_000


def test_happy_path_returns_user_id() -> None:
    token = _make_token({"exp": _NOW + 60, "preferred_username": "alice"})
    assert decode_jwt_payload(token, claim_user_id="preferred_username", now=_NOW) == "alice"


def test_custom_claim_name_round_trip() -> None:
    token = _make_token({"exp": _NOW + 60, "sub": "bob"})
    assert decode_jwt_payload(token, claim_user_id="sub", now=_NOW) == "bob"


def test_expired_exp_raises_token_expired() -> None:
    token = _make_token({"exp": _NOW - 1, "preferred_username": "alice"})
    with pytest.raises(JwtAuthError) as excinfo:
        decode_jwt_payload(token, claim_user_id="preferred_username", now=_NOW)
    assert excinfo.value.error_code == "AUTH_TOKEN_EXPIRED"
    assert excinfo.value.http_status == 401


def test_exp_equal_to_now_is_expired() -> None:
    token = _make_token({"exp": _NOW, "preferred_username": "alice"})
    with pytest.raises(JwtAuthError) as excinfo:
        decode_jwt_payload(token, claim_user_id="preferred_username", now=_NOW)
    assert excinfo.value.error_code == "AUTH_TOKEN_EXPIRED"


def test_missing_exp_raises_claim_missing() -> None:
    token = _make_token({"preferred_username": "alice"})
    with pytest.raises(JwtAuthError) as excinfo:
        decode_jwt_payload(token, claim_user_id="preferred_username", now=_NOW)
    assert excinfo.value.error_code == "AUTH_CLAIM_MISSING"


def test_missing_user_claim_raises_claim_missing() -> None:
    token = _make_token({"exp": _NOW + 60})
    with pytest.raises(JwtAuthError) as excinfo:
        decode_jwt_payload(token, claim_user_id="preferred_username", now=_NOW)
    assert excinfo.value.error_code == "AUTH_CLAIM_MISSING"


def test_empty_user_claim_raises_claim_missing() -> None:
    token = _make_token({"exp": _NOW + 60, "preferred_username": ""})
    with pytest.raises(JwtAuthError) as excinfo:
        decode_jwt_payload(token, claim_user_id="preferred_username", now=_NOW)
    assert excinfo.value.error_code == "AUTH_CLAIM_MISSING"


def test_non_string_user_claim_raises_claim_missing() -> None:
    token = _make_token({"exp": _NOW + 60, "preferred_username": 12345})
    with pytest.raises(JwtAuthError) as excinfo:
        decode_jwt_payload(token, claim_user_id="preferred_username", now=_NOW)
    assert excinfo.value.error_code == "AUTH_CLAIM_MISSING"


def test_non_numeric_exp_raises_token_invalid() -> None:
    token = _make_token({"exp": "soon", "preferred_username": "alice"})
    with pytest.raises(JwtAuthError) as excinfo:
        decode_jwt_payload(token, claim_user_id="preferred_username", now=_NOW)
    assert excinfo.value.error_code == "AUTH_TOKEN_INVALID"


def test_token_with_two_segments_raises_token_invalid() -> None:
    token = "abc.def"  # missing signature segment
    with pytest.raises(JwtAuthError) as excinfo:
        decode_jwt_payload(token, claim_user_id="preferred_username", now=_NOW)
    assert excinfo.value.error_code == "AUTH_TOKEN_INVALID"


def test_token_with_non_base64_payload_raises_token_invalid() -> None:
    token = "abc.!!!not-base64!!!.sig"
    with pytest.raises(JwtAuthError) as excinfo:
        decode_jwt_payload(token, claim_user_id="preferred_username", now=_NOW)
    assert excinfo.value.error_code == "AUTH_TOKEN_INVALID"


def test_token_with_non_json_payload_raises_token_invalid() -> None:
    payload_b64 = _b64url(b"not json at all")
    token = f"abc.{payload_b64}.sig"
    with pytest.raises(JwtAuthError) as excinfo:
        decode_jwt_payload(token, claim_user_id="preferred_username", now=_NOW)
    assert excinfo.value.error_code == "AUTH_TOKEN_INVALID"


def test_token_with_array_payload_raises_token_invalid() -> None:
    payload_b64 = _b64url(json.dumps([1, 2, 3]).encode())
    token = f"abc.{payload_b64}.sig"
    with pytest.raises(JwtAuthError) as excinfo:
        decode_jwt_payload(token, claim_user_id="preferred_username", now=_NOW)
    assert excinfo.value.error_code == "AUTH_TOKEN_INVALID"


def test_empty_token_raises_token_invalid() -> None:
    with pytest.raises(JwtAuthError) as excinfo:
        decode_jwt_payload("", claim_user_id="preferred_username", now=_NOW)
    assert excinfo.value.error_code == "AUTH_TOKEN_INVALID"


def test_signature_not_verified() -> None:
    """§3.5.1 accepted risk: signature is intentionally ignored.

    A token with a bogus signature segment still decodes successfully so long
    as exp and the claim are valid — this asserts the documented weakening.
    """
    token = _make_token(
        {"exp": _NOW + 60, "preferred_username": "alice"},
        signature="completely-bogus-not-a-real-signature",
    )
    assert decode_jwt_payload(token, claim_user_id="preferred_username", now=_NOW) == "alice"
