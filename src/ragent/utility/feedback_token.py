"""HMAC-signed snapshot token for `POST /feedback/v1` (B51, T-FB.1).

Format: ``<payload_base64url_nopad>.<hmac_sha256_hex>``.

The payload binds (`request_id`, `user_id`, `sources_hash`, `ts`); any
modification breaks the HMAC. ``ts`` is epoch seconds; tokens outside the
7-day window (past or future, beyond a 60s clock-skew buffer) are rejected.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import re
import time
from hashlib import sha256

TTL_SECONDS = 7 * 86400
CLOCK_SKEW_TOLERANCE_SECONDS = 60
_REQUIRED_KEYS = frozenset({"request_id", "user_id", "sources_hash", "ts"})
_BASE64URL_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


class FeedbackTokenError(Exception):
    """Base class for `sign`/`verify` failures — catch this to handle all."""


class TokenInvalid(FeedbackTokenError):
    """Malformed token, invalid base64, or payload missing required keys."""


class TokenTampered(FeedbackTokenError):
    """HMAC mismatch — wrong secret or modified bytes."""


class TokenExpired(FeedbackTokenError):
    """Token ``ts`` outside the validity window (past TTL or future skew)."""


def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def compute_sources_hash(source_refs: list[tuple[str, str]]) -> str:
    """SHA-256 over the ordered list of ``(source_app, source_id)`` pairs — bound
    into the HMAC payload's ``sources_hash`` field (B51).

    Document identity is the **pair** ``(source_app, source_id)`` per B11/B35;
    binding ``source_id`` alone would let a client forge the ``source_app``
    component of the same vote (PR #80 review, gemini-code-assist
    security-high). Each entry is serialised as a JSON array ``[app, id]`` so
    chat-side mint and feedback-side verify produce byte-identical inputs.

    Both call sites (``routers/chat`` mint, ``routers/feedback`` verify) must
    pass pairs in the same order.
    """
    payload = [[app, sid] for app, sid in source_refs]
    return sha256(json.dumps(payload, separators=(",", ":")).encode("utf-8")).hexdigest()


def _mac(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), body, sha256).hexdigest()


def sign(payload: dict, secret: str) -> str:
    if not _REQUIRED_KEYS.issubset(payload.keys()):
        missing = _REQUIRED_KEYS - payload.keys()
        raise TokenInvalid(f"payload missing required keys: {sorted(missing)}")
    body = _canonical(payload)
    body_b64 = base64.urlsafe_b64encode(body).decode("ascii").rstrip("=")
    return f"{body_b64}.{_mac(body, secret)}"


def verify(token: str, secret: str) -> dict:
    if not token or token.count(".") != 1:
        raise TokenInvalid("token must be '<body>.<mac>'")
    body_b64, mac = token.split(".", 1)
    if not body_b64 or not mac or not _BASE64URL_RE.match(body_b64):
        raise TokenInvalid("body has invalid characters")
    try:
        body = base64.urlsafe_b64decode(body_b64 + "=" * (-len(body_b64) % 4))
    except (ValueError, binascii.Error) as exc:
        raise TokenInvalid("body is not valid base64url") from exc
    if not hmac.compare_digest(_mac(body, secret), mac):
        raise TokenTampered("HMAC mismatch")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise TokenInvalid("payload is not valid JSON") from exc
    if not isinstance(payload, dict) or not _REQUIRED_KEYS.issubset(payload.keys()):
        raise TokenInvalid("payload missing required keys")
    ts = payload.get("ts")
    if not isinstance(ts, int):
        raise TokenInvalid("ts must be int seconds")
    now = int(time.time())
    if ts > now + CLOCK_SKEW_TOLERANCE_SECONDS:
        raise TokenExpired("ts is in the future")
    if now - ts > TTL_SECONDS:
        raise TokenExpired(f"ts is older than {TTL_SECONDS // 86400}-day TTL")
    return payload
