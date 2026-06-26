"""Tests for ASTCipher (T-CAT.5) — AES-256-GCM encrypt/decrypt of AST artifacts."""

import os

import pytest

from ragent.security.ast_cipher import ASTCipher, ASTDecryptionError


class _FakeKeyManager:
    """Stand-in for KeyManager — ASTCipher depends only on `.dek` (ISP)."""

    def __init__(self, dek: bytes) -> None:
        self.dek = dek


@pytest.fixture
def cipher() -> ASTCipher:
    key_manager = _FakeKeyManager(dek=os.urandom(32))
    return ASTCipher(key_manager)


def test_encrypt_ast_round_trip(cipher: ASTCipher):
    plaintext = '{"type": "document", "sections": []}'
    envelope = cipher.encrypt_ast(plaintext)

    decrypted = cipher.decrypt_ast(envelope)

    assert decrypted == plaintext


def test_encrypt_ast_envelope_shape(cipher: ASTCipher):
    envelope = cipher.encrypt_ast("plain text content")

    assert envelope["version"] == "1.0"
    assert envelope["algorithm"] == "AES-256-GCM"
    assert "nonce" in envelope
    assert "ciphertext" in envelope
    assert "metadata" not in envelope


def test_encrypt_ast_nonce_is_random_per_call(cipher: ASTCipher):
    e1 = cipher.encrypt_ast("same content")
    e2 = cipher.encrypt_ast("same content")

    assert e1["nonce"] != e2["nonce"]
    assert e1["ciphertext"] != e2["ciphertext"]


def test_decrypt_ast_tamper_detection(cipher: ASTCipher):
    """A modified ciphertext must fail the GCM tag check, not decrypt silently."""
    envelope = cipher.encrypt_ast("sensitive content")
    tampered = dict(envelope)
    # Flip a hex character in the ciphertext to simulate tampering.
    tampered["ciphertext"] = ("0" if tampered["ciphertext"][0] != "0" else "1") + tampered[
        "ciphertext"
    ][1:]

    with pytest.raises(ASTDecryptionError):
        cipher.decrypt_ast(tampered)


def test_decrypt_ast_wrong_key_fails():
    """A cipher built with a different DEK cannot decrypt another's ciphertext."""
    cipher_a = ASTCipher(_FakeKeyManager(dek=os.urandom(32)))
    cipher_b = ASTCipher(_FakeKeyManager(dek=os.urandom(32)))

    envelope = cipher_a.encrypt_ast("secret")

    with pytest.raises(ASTDecryptionError):
        cipher_b.decrypt_ast(envelope)


def test_cipher_depends_only_on_dek_attribute():
    """ASTCipher must work with any object exposing `.dek` (ISP boundary)."""
    key_manager = _FakeKeyManager(dek=os.urandom(32))
    cipher = ASTCipher(key_manager)
    envelope = cipher.encrypt_ast("x")
    assert cipher.decrypt_ast(envelope) == "x"
