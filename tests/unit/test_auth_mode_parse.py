"""T-AM.S1 — AuthMode enum and parse_auth_mode() unit tests."""

import pytest


def test_enum_values() -> None:
    from ragent.bootstrap.auth_mode import AuthMode

    assert AuthMode.none == "none"
    assert AuthMode.user_header == "user_header"
    assert AuthMode.jwt_header == "jwt_header"
    assert AuthMode.jwt_prefer_header == "jwt_prefer_header"


def test_parse_explicit_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_AUTH_MODE", "none")
    from ragent.bootstrap.auth_mode import AuthMode, parse_auth_mode

    assert parse_auth_mode() == AuthMode.none


def test_parse_explicit_user_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_AUTH_MODE", "user_header")
    from ragent.bootstrap.auth_mode import AuthMode, parse_auth_mode

    assert parse_auth_mode() == AuthMode.user_header


def test_parse_explicit_jwt_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_AUTH_MODE", "jwt_header")
    from ragent.bootstrap.auth_mode import AuthMode, parse_auth_mode

    assert parse_auth_mode() == AuthMode.jwt_header


def test_parse_explicit_jwt_prefer_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_AUTH_MODE", "jwt_prefer_header")
    from ragent.bootstrap.auth_mode import AuthMode, parse_auth_mode

    assert parse_auth_mode() == AuthMode.jwt_prefer_header


def test_parse_default_is_user_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RAGENT_AUTH_MODE", raising=False)
    from ragent.bootstrap.auth_mode import AuthMode, parse_auth_mode

    assert parse_auth_mode() == AuthMode.user_header


def test_parse_unknown_value_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_AUTH_MODE", "bogus")
    from ragent.bootstrap.auth_mode import parse_auth_mode

    with pytest.raises(ValueError, match="bogus"):
        parse_auth_mode()


def test_parse_strips_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_AUTH_MODE", "  jwt_header  ")
    from ragent.bootstrap.auth_mode import AuthMode, parse_auth_mode

    assert parse_auth_mode() == AuthMode.jwt_header


def test_parse_uppercase_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_AUTH_MODE", "JWT_HEADER")
    from ragent.bootstrap.auth_mode import parse_auth_mode

    with pytest.raises(ValueError, match="JWT_HEADER"):
        parse_auth_mode()
