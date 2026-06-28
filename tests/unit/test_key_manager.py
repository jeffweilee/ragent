"""Tests for KeyManager (T-CAT.4) — process-wide DEK unwrap from KEK."""

import base64
import os

import pytest
from cryptography.hazmat.primitives.keywrap import aes_key_wrap

from ragent.security.key_manager import KeyManager, KeyManagerError


def _make_kek_and_wrapped_dek() -> tuple[bytes, bytes, bytes]:
    """Build a (kek, dek, wrapped_dek) triple for tests."""
    kek = os.urandom(32)
    dek = os.urandom(32)
    wrapped_dek = aes_key_wrap(kek, dek)
    return kek, dek, wrapped_dek


def test_key_manager_unwraps_dek_round_trip():
    """KeyManager unwraps the DEK from KEK + encrypted DEK correctly."""
    kek, dek, wrapped_dek = _make_kek_and_wrapped_dek()
    kek_b64 = base64.b64encode(kek).decode()
    wrapped_dek_b64 = base64.b64encode(wrapped_dek).decode()

    manager = KeyManager(kek_b64=kek_b64, encrypted_dek_b64=wrapped_dek_b64)

    assert manager.dek == dek


def test_key_manager_dek_is_bytes():
    kek, dek, wrapped_dek = _make_kek_and_wrapped_dek()
    kek_b64 = base64.b64encode(kek).decode()
    wrapped_dek_b64 = base64.b64encode(wrapped_dek).decode()

    manager = KeyManager(kek_b64=kek_b64, encrypted_dek_b64=wrapped_dek_b64)

    assert isinstance(manager.dek, bytes)
    assert len(manager.dek) == 32


def test_key_manager_bad_kek_raises():
    """Unwrapping with the wrong KEK must fail loudly, not silently."""
    _, _, wrapped_dek = _make_kek_and_wrapped_dek()
    wrong_kek = os.urandom(32)
    kek_b64 = base64.b64encode(wrong_kek).decode()
    wrapped_dek_b64 = base64.b64encode(wrapped_dek).decode()

    with pytest.raises(KeyManagerError):
        KeyManager(kek_b64=kek_b64, encrypted_dek_b64=wrapped_dek_b64)


def test_key_manager_malformed_base64_raises():
    """Malformed base64 input must raise KeyManagerError, not a raw exception."""
    with pytest.raises(KeyManagerError):
        KeyManager(kek_b64="not-valid-base64!!!", encrypted_dek_b64="also-not-valid!!!")


def test_key_manager_dek_immutable_across_calls():
    """The same KeyManager instance must return the same dek bytes (process-wide)."""
    kek, dek, wrapped_dek = _make_kek_and_wrapped_dek()
    kek_b64 = base64.b64encode(kek).decode()
    wrapped_dek_b64 = base64.b64encode(wrapped_dek).decode()

    manager = KeyManager(kek_b64=kek_b64, encrypted_dek_b64=wrapped_dek_b64)

    assert manager.dek == manager.dek == dek


def test_key_manager_wrap_round_trips_with_unwrap():
    """KeyManager.wrap() output must unwrap back to the original DEK via KeyManager()."""
    kek = os.urandom(32)
    dek = os.urandom(32)
    kek_b64 = base64.b64encode(kek).decode()

    wrapped_dek_b64 = KeyManager.wrap(kek_b64, dek)

    manager = KeyManager(kek_b64=kek_b64, encrypted_dek_b64=wrapped_dek_b64)
    assert manager.dek == dek


def test_key_manager_wrap_matches_aes_key_wrap():
    """KeyManager.wrap() must produce the same bytes as the raw primitive."""
    kek = os.urandom(32)
    dek = os.urandom(32)
    kek_b64 = base64.b64encode(kek).decode()

    wrapped_dek_b64 = KeyManager.wrap(kek_b64, dek)

    assert base64.b64decode(wrapped_dek_b64) == aes_key_wrap(kek, dek)
