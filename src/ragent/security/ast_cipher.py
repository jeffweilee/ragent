"""ASTCipher — AES-256-GCM encryption for chat-attachment AST artifacts (T-CAT.5).

Depends only on a `.dek` attribute (Interface Segregation) — it never sees
the KEK or the wrap/unwrap mechanics that produced the DEK.
"""

from __future__ import annotations

import os
from typing import Any, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_BYTES = 12
_ENVELOPE_VERSION = "1.0"
_ALGORITHM = "AES-256-GCM"


class ASTDecryptionError(Exception):
    """Raised when decryption fails — tampered ciphertext or wrong key."""


class _HasDek(Protocol):
    dek: bytes


class ASTCipher:
    """AES-256-GCM encrypt/decrypt for AST artifacts, keyed by `KeyManager.dek`."""

    def __init__(self, key_manager: _HasDek) -> None:
        self._aesgcm = AESGCM(key_manager.dek)

    def encrypt_ast(
        self, plaintext: str, *, attachment_id: str, ast_type: str, created_at: str
    ) -> dict[str, Any]:
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = self._aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
        return {
            "version": _ENVELOPE_VERSION,
            "algorithm": _ALGORITHM,
            "nonce": nonce.hex(),
            "ciphertext": ciphertext.hex(),
            "metadata": {
                "attachment_id": attachment_id,
                "ast_type": ast_type,
                "created_at": created_at,
            },
        }

    def decrypt_ast(self, envelope: dict[str, Any]) -> str:
        try:
            nonce = bytes.fromhex(envelope["nonce"])
            ciphertext = bytes.fromhex(envelope["ciphertext"])
            plaintext = self._aesgcm.decrypt(nonce, ciphertext, None)
        except (InvalidTag, ValueError) as exc:
            raise ASTDecryptionError(f"failed to decrypt AST: {exc}") from exc
        return plaintext.decode("utf-8")
