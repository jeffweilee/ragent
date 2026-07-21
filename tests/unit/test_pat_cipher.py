"""T-PAT.2 — PATCipher AES-256-GCM round-trip + tamper detection."""

from __future__ import annotations

import os

import pytest

from ragent.security.pat_cipher import PATCipher, PATDecryptionError


class _StubKeyManager:
    """Only exposes `.dek` (Interface Segregation — same as ASTCipher)."""

    def __init__(self) -> None:
        self.dek = os.urandom(32)


def _cipher() -> PATCipher:
    return PATCipher(_StubKeyManager())


def test_encrypt_decrypt_round_trips() -> None:
    cipher = _cipher()
    plaintext = "eyJhbGciOiJSUzI1NiJ9.payload.sig"

    envelope = cipher.encrypt(plaintext)

    assert cipher.decrypt(envelope) == plaintext


def test_ciphertext_is_not_plaintext_and_is_a_string() -> None:
    cipher = _cipher()

    envelope = cipher.encrypt("secret-pat")

    assert isinstance(envelope, str)
    assert "secret-pat" not in envelope


def test_encrypt_is_nondeterministic() -> None:
    cipher = _cipher()

    assert cipher.encrypt("x") != cipher.encrypt("x")


def test_tampered_ciphertext_raises() -> None:
    cipher = _cipher()
    envelope = cipher.encrypt("secret-pat")

    version, nonce_b64, ct_b64 = envelope.split(".")
    flipped = ct_b64[:-2] + ("AA" if ct_b64[-2:] != "AA" else "BB")
    tampered = f"{version}.{nonce_b64}.{flipped}"

    with pytest.raises(PATDecryptionError):
        cipher.decrypt(tampered)


def test_wrong_key_raises() -> None:
    envelope = _cipher().encrypt("secret-pat")

    with pytest.raises(PATDecryptionError):
        _cipher().decrypt(envelope)  # a different DEK


@pytest.mark.parametrize("bad", ["", "not-an-envelope", "v1.only-two"])
def test_malformed_envelope_raises(bad: str) -> None:
    with pytest.raises(PATDecryptionError):
        _cipher().decrypt(bad)
