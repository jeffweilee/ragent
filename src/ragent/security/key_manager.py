"""KeyManager — process-wide DEK unwrap (T-CAT.4).

Unwraps a single Data Encryption Key (DEK) from a Key Encryption Key (KEK)
once at construction; the DEK is held in memory for the process lifetime.
ISP: callers only ever see `.dek` — the KEK and the wrap/unwrap mechanics
never leak past this module.
"""

from __future__ import annotations

import base64

from cryptography.hazmat.primitives.keywrap import (
    InvalidUnwrap,
    aes_key_unwrap,
    aes_key_wrap,
)


class KeyManagerError(Exception):
    """Raised when the DEK cannot be unwrapped (bad KEK or malformed input)."""


class KeyManager:
    """Unwraps and holds the process-wide DEK."""

    def __init__(self, *, kek_b64: str, encrypted_dek_b64: str) -> None:
        try:
            kek = base64.b64decode(kek_b64, validate=True)
            wrapped_dek = base64.b64decode(encrypted_dek_b64, validate=True)
            self._dek = aes_key_unwrap(kek, wrapped_dek)
        except (InvalidUnwrap, ValueError) as exc:
            raise KeyManagerError(f"failed to unwrap DEK: {exc}") from exc

    @property
    def dek(self) -> bytes:
        return self._dek

    @staticmethod
    def wrap(kek_b64: str, dek: bytes) -> str:
        """Wrap a DEK under a KEK. Returns the base64-encoded wrapped DEK.

        Used by `scripts/gen_attachment_keys.py` (generate/rotate) so the
        `aes_key_wrap` mechanics stay confined to this module.
        """
        kek = base64.b64decode(kek_b64, validate=True)
        return base64.b64encode(aes_key_wrap(kek, dek)).decode()
