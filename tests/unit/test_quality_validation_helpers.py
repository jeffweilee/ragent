"""Unit tests for pure helper functions in _quality_validation.py."""

from __future__ import annotations

import base64
import json
import tempfile

import yaml
from twp_ai.schemas import Message

from ragent.routers._quality_validation import (
    _decode_jwt_claim,
    is_admin_user,
    is_admin_validation_command,
    load_questions,
)

# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------


def _make_jwt(payload: dict) -> str:
    """Build a fake JWT (unsigned) for testing."""
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"Bearer {header}.{body}.fakesig"


# ---------------------------------------------------------------------------
# _decode_jwt_claim
# ---------------------------------------------------------------------------


def test_decode_jwt_claim_returns_claim_value() -> None:
    token = _make_jwt({"sub": "user-123"})
    assert _decode_jwt_claim(token, "sub") == "user-123"


def test_decode_jwt_claim_custom_claim() -> None:
    token = _make_jwt({"sub": "user-123", "uid": "admin-456"})
    assert _decode_jwt_claim(token, "uid") == "admin-456"


def test_decode_jwt_claim_absent_claim_returns_none() -> None:
    token = _make_jwt({"sub": "user-123"})
    assert _decode_jwt_claim(token, "uid") is None


def test_decode_jwt_claim_empty_header_returns_none() -> None:
    assert _decode_jwt_claim("", "sub") is None


def test_decode_jwt_claim_malformed_token_returns_none() -> None:
    assert _decode_jwt_claim("Bearer notajwt", "sub") is None


def test_decode_jwt_claim_missing_bearer_prefix_still_works() -> None:
    payload = {"sub": "user-123"}
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    raw = f"{header}.{body}.fakesig"
    # Without "Bearer " prefix — removeprefix leaves it unchanged, split still works
    assert _decode_jwt_claim(raw, "sub") == "user-123"


# ---------------------------------------------------------------------------
# is_admin_user
# ---------------------------------------------------------------------------


def test_is_admin_user_returns_true_for_known_id() -> None:
    token = _make_jwt({"sub": "admin-1"})
    assert is_admin_user(token, ["admin-1", "admin-2"], "sub") is True


def test_is_admin_user_returns_false_for_unknown_id() -> None:
    token = _make_jwt({"sub": "stranger"})
    assert is_admin_user(token, ["admin-1"], "sub") is False


def test_is_admin_user_empty_admin_list_returns_false() -> None:
    token = _make_jwt({"sub": "admin-1"})
    assert is_admin_user(token, [], "sub") is False


def test_is_admin_user_empty_auth_header_returns_false() -> None:
    assert is_admin_user("", ["admin-1"], "sub") is False


def test_is_admin_user_uses_configured_claim() -> None:
    token = _make_jwt({"sub": "user-99", "uid": "admin-1"})
    assert is_admin_user(token, ["admin-1"], "uid") is True
    assert is_admin_user(token, ["admin-1"], "sub") is False


def test_is_admin_user_wrong_claim_absent_returns_false() -> None:
    token = _make_jwt({"sub": "admin-1"})
    # jwt_claim is "uid" but the token only has "sub"
    assert is_admin_user(token, ["admin-1"], "uid") is False


# ---------------------------------------------------------------------------
# is_admin_validation_command
# ---------------------------------------------------------------------------


def _msg(role: str, content: str) -> Message:
    return Message(role=role, content=content)


def test_is_admin_validation_command_returns_true_for_command() -> None:
    msgs = [_msg("user", "/admin-quality-validation")]
    assert is_admin_validation_command(msgs) is True


def test_is_admin_validation_command_trims_whitespace() -> None:
    msgs = [_msg("user", "  /admin-quality-validation  ")]
    assert is_admin_validation_command(msgs) is True


def test_is_admin_validation_command_returns_false_for_other_message() -> None:
    msgs = [_msg("user", "hello")]
    assert is_admin_validation_command(msgs) is False


def test_is_admin_validation_command_checks_last_user_message() -> None:
    msgs = [
        _msg("user", "/admin-quality-validation"),
        _msg("assistant", "response"),
        _msg("user", "follow up"),
    ]
    assert is_admin_validation_command(msgs) is False


def test_is_admin_validation_command_empty_messages_returns_false() -> None:
    assert is_admin_validation_command([]) is False


def test_is_admin_validation_command_no_user_messages_returns_false() -> None:
    msgs = [_msg("assistant", "hi")]
    assert is_admin_validation_command(msgs) is False


def test_is_admin_validation_command_structured_content_returns_false() -> None:
    msg = Message(role="user", content=[{"type": "text", "text": "/admin-quality-validation"}])
    assert is_admin_validation_command([msg]) is False


# ---------------------------------------------------------------------------
# load_questions
# ---------------------------------------------------------------------------


def test_load_questions_returns_questions_list() -> None:
    fixture = {"questions": [{"id": "q1", "label": "test", "question": "hello?"}]}
    with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
        yaml.dump(fixture, f)
        path = f.name
    result = load_questions(path)
    assert len(result) == 1
    assert result[0]["id"] == "q1"


def test_load_questions_missing_file_returns_empty() -> None:
    result = load_questions("/nonexistent/path/fixture.yaml")
    assert result == []


def test_load_questions_empty_questions_key_returns_empty() -> None:
    fixture = {"questions": []}
    with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
        yaml.dump(fixture, f)
        path = f.name
    assert load_questions(path) == []


def test_load_questions_empty_yaml_file_returns_empty() -> None:
    with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
        f.write("")  # empty file — yaml.safe_load returns None
        path = f.name
    assert load_questions(path) == []
