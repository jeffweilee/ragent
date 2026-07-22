"""PATCipher — AES-256-GCM encryption for stored Personal Access Tokens (T-PAT).

Mirrors `ASTCipher` (same AES-256-GCM, same `KeyManager.dek` seam via Interface
Segregation — it never sees the KEK), but emits a **compact string** envelope
(`v1.<nonce_b64>.<ciphertext_b64>`) instead of a dict, because a PAT is stored
as a single string in the `pat.pat_cipher` column and cached as a redis string.
"""

from __future__ import annotations

import base64
import os
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_BYTES = 12
_ENVELOPE_VERSION = "v1"


class PATDecryptionError(Exception):
    """Raised when decryption fails — tampered ciphertext, wrong key, or a
    malformed envelope."""


class _HasDek(Protocol):
    dek: bytes


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text.encode("ascii"))


class PATCipher:
    """AES-256-GCM encrypt/decrypt for PAT strings, keyed by `KeyManager.dek`."""

    def __init__(self, key_manager: _HasDek) -> None:
        self._aesgcm = AESGCM(key_manager.dek)

    def encrypt(self, plaintext: str) -> str:
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = self._aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
        return f"{_ENVELOPE_VERSION}.{_b64e(nonce)}.{_b64e(ciphertext)}"

    def decrypt(self, envelope: str) -> str:
        try:
            version, nonce_b64, ct_b64 = envelope.split(".")
            if version != _ENVELOPE_VERSION:
                raise ValueError(f"unsupported envelope version {version!r}")
            plaintext = self._aesgcm.decrypt(_b64d(nonce_b64), _b64d(ct_b64), None)
        except (ValueError, InvalidTag) as exc:
            raise PATDecryptionError(f"failed to decrypt PAT: {exc}") from exc
        return plaintext.decode("utf-8")
