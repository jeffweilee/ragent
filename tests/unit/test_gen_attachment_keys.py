"""Tests for scripts/gen_attachment_keys.py — KEK/DEK generate + rotate."""

from __future__ import annotations

import importlib.util
import types
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "gen_attachment_keys.py"


def _load_script() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("gen_attachment_keys", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script() -> types.ModuleType:
    return _load_script()


def test_generate_round_trips_through_key_manager(script):
    """A freshly generated KEK/DEK pair must unwrap cleanly via KeyManager."""
    from ragent.security.key_manager import KeyManager

    kek_b64, encrypted_dek_b64 = script.generate()

    key_manager = KeyManager(kek_b64=kek_b64, encrypted_dek_b64=encrypted_dek_b64)
    assert len(key_manager.dek) == 32


def test_generate_is_random_per_call(script):
    kek1, dek1 = script.generate()
    kek2, dek2 = script.generate()

    assert kek1 != kek2
    assert dek1 != dek2


def test_rotate_preserves_dek(script):
    """Rotation must re-wrap the SAME DEK under a new KEK (no re-encryption needed)."""
    from ragent.security.key_manager import KeyManager

    old_kek_b64, old_encrypted_dek_b64 = script.generate()
    old_dek = KeyManager(kek_b64=old_kek_b64, encrypted_dek_b64=old_encrypted_dek_b64).dek

    new_kek_b64, new_encrypted_dek_b64 = script.rotate(old_kek_b64, old_encrypted_dek_b64)
    new_dek = KeyManager(kek_b64=new_kek_b64, encrypted_dek_b64=new_encrypted_dek_b64).dek

    assert new_dek == old_dek
    assert new_kek_b64 != old_kek_b64
    assert new_encrypted_dek_b64 != old_encrypted_dek_b64


def test_rotate_rejects_wrong_kek(script):
    from ragent.security.key_manager import KeyManagerError

    _, encrypted_dek_b64 = script.generate()
    wrong_kek_b64, _ = script.generate()

    with pytest.raises(KeyManagerError):
        script.rotate(wrong_kek_b64, encrypted_dek_b64)


def test_main_generate_prints_env_lines(script, capsys):
    exit_code = script.main(["generate"])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "RAGENT_KEK_BASE64=" in out
    assert "RAGENT_ENCRYPTED_DEK_BASE64=" in out


def test_main_rotate_prints_env_lines(script, capsys):
    old_kek_b64, old_encrypted_dek_b64 = script.generate()

    exit_code = script.main(
        ["rotate", "--old-kek", old_kek_b64, "--old-encrypted-dek", old_encrypted_dek_b64]
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "RAGENT_KEK_BASE64=" in out
    assert "RAGENT_ENCRYPTED_DEK_BASE64=" in out


def test_main_rotate_invalid_kek_exits_nonzero(script, capsys):
    _, encrypted_dek_b64 = script.generate()

    exit_code = script.main(
        ["rotate", "--old-kek", "not-valid-base64!!", "--old-encrypted-dek", encrypted_dek_b64]
    )

    assert exit_code == 1
    assert "error:" in capsys.readouterr().err
