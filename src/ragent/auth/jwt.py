"""T8.2 — decode-only JWT payload extraction (§3.5 / §3.5.1).

Accepted risk: the signature segment is **not** verified. The deployment
contract (§3.5.1) is that an upstream gateway authenticates the caller and
forwards the already-vetted JWT in ``<RAGENT_JWT_HEADER>``. ragent extracts
``exp`` (expiry check only) and the configured user_id claim.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass

from ragent.errors.codes import HttpErrorCode


@dataclass(frozen=True)
class JwtAuthError(Exception):
    """Raised by :func:`decode_jwt_payload` on any decode/claim failure.

    Surfaces via the global problem-details handler.
    """

    error_code: HttpErrorCode
    http_status: int = 401

    def __str__(self) -> str:
        return self.error_code


def decode_jwt_payload(token: str, *, claim_user_id: str, now: int) -> str:
    """Decode a JWT, check ``exp``, and return the configured claim value.

    Args:
        token: Raw JWT (three base64url segments separated by ``.``).
        claim_user_id: Payload claim path used as the downstream user_id.
        now: Current unix epoch — injected to keep the function pure /
            testable. Callers pass ``int(time.time())``.

    Returns:
        The non-empty string value of ``payload[claim_user_id]``.

    Raises:
        JwtAuthError: on any failure path enumerated in §3.5.
    """
    segments = token.split(".") if token else []
    if len(segments) != 3:
        raise JwtAuthError(HttpErrorCode.AUTH_TOKEN_INVALID)

    body_b64 = segments[1]
    try:
        payload_bytes = base64.urlsafe_b64decode(body_b64 + "=" * (-len(body_b64) % 4))
    except (binascii.Error, ValueError):
        raise JwtAuthError(HttpErrorCode.AUTH_TOKEN_INVALID) from None

    try:
        payload = json.loads(payload_bytes)
    except json.JSONDecodeError:
        raise JwtAuthError(HttpErrorCode.AUTH_TOKEN_INVALID) from None

    if not isinstance(payload, dict):
        raise JwtAuthError(HttpErrorCode.AUTH_TOKEN_INVALID)

    if "exp" not in payload:
        raise JwtAuthError(HttpErrorCode.AUTH_CLAIM_MISSING)

    exp = payload["exp"]
    # bools are an int subclass in Python; reject them explicitly.
    if isinstance(exp, bool) or not isinstance(exp, int):
        raise JwtAuthError(HttpErrorCode.AUTH_TOKEN_INVALID)

    if exp <= now:
        raise JwtAuthError(HttpErrorCode.AUTH_TOKEN_EXPIRED)

    user_id = payload.get(claim_user_id)
    if not isinstance(user_id, str) or not user_id:
        raise JwtAuthError(HttpErrorCode.AUTH_CLAIM_MISSING)

    return user_id
