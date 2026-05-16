"""TDD — feedback_token.sign/verify: HMAC-signed snapshot token (B51, T-FB.1).

Pins the contract for the HMAC-signed feedback token surfaced in `/chat`
responses and echoed back in `POST /feedback/v1`. Payload binds
(request_id, user_id, sources_hash, ts); TTL = 7 days; tamper/expiry/
malformed-input all raise distinct exceptions.
"""

from __future__ import annotations

import time

import pytest

from ragent.utility.feedback_token import (
    TokenExpired,
    TokenInvalid,
    TokenTampered,
    sign,
    verify,
)

SECRET = "test-secret-do-not-use-in-prod"
PAYLOAD = {
    "request_id": "01JABCDEFGHIJKLMNOPQRSTUVW",
    "user_id": "alice",
    "sources_hash": "abc123def456" * 4,  # sha256-hex shape, 48 chars
    "ts": int(time.time()),
}


def test_roundtrip_returns_same_payload():
    token = sign(PAYLOAD, SECRET)
    assert verify(token, SECRET) == PAYLOAD


def test_tamper_single_byte_detected():
    token = sign(PAYLOAD, SECRET)
    # flip the last character (preserving length / format)
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    with pytest.raises(TokenTampered):
        verify(tampered, SECRET)


def test_expired_ts_older_than_7d_raises():
    eight_days_ago = int(time.time()) - 8 * 86400
    token = sign({**PAYLOAD, "ts": eight_days_ago}, SECRET)
    with pytest.raises(TokenExpired):
        verify(token, SECRET)


def test_wrong_secret_raises_tampered():
    token = sign(PAYLOAD, SECRET)
    with pytest.raises(TokenTampered):
        verify(token, "different-secret")


def test_malformed_token_raises_invalid():
    with pytest.raises(TokenInvalid):
        verify("", SECRET)
    with pytest.raises(TokenInvalid):
        verify("nodot", SECRET)
    with pytest.raises(TokenInvalid):
        verify("too.many.dots.here", SECRET)
    with pytest.raises(TokenInvalid):
        verify("not_base64!!!.deadbeef", SECRET)
    # Empty half on either side of the dot — single-dot count passes the
    # first guard so the empty-half guard must catch it.
    with pytest.raises(TokenInvalid):
        verify(".", SECRET)
    with pytest.raises(TokenInvalid):
        verify(".validmac", SECRET)
    with pytest.raises(TokenInvalid):
        verify("validbody.", SECRET)


def test_just_inside_7d_window_still_valid():
    almost_7d_ago = int(time.time()) - 7 * 86400 + 60  # 60s inside the window
    token = sign({**PAYLOAD, "ts": almost_7d_ago}, SECRET)
    payload = verify(token, SECRET)
    assert payload["ts"] == almost_7d_ago


def test_future_ts_rejected():
    """Token whose ts is in the future (clock skew or forgery) is rejected."""
    one_hour_future = int(time.time()) + 3600
    token = sign({**PAYLOAD, "ts": one_hour_future}, SECRET)
    with pytest.raises(TokenExpired):
        verify(token, SECRET)


def test_sign_rejects_missing_required_keys():
    with pytest.raises(TokenInvalid):
        sign({"request_id": "x", "user_id": "y"}, SECRET)  # missing sources_hash, ts
    with pytest.raises(TokenInvalid):
        sign({}, SECRET)


def test_verify_rejects_non_int_ts():
    """Sign accepts any-typed ts; verify must enforce int."""
    token = sign({**PAYLOAD, "ts": "1234567890"}, SECRET)  # type: ignore[dict-item]
    with pytest.raises(TokenInvalid):
        verify(token, SECRET)
