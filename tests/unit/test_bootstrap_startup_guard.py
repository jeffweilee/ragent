"""T-AM.1 — Startup guard: RAGENT_AUTH_MODE coherence and safety constraints.

Four modes:
  none             — dev only
  user_header      — dev only
  jwt_header       — no env restriction; requires OIDC_DOMAIN + OIDC_AUDIENCE
  jwt_prefer_header— dev only; requires OIDC_DOMAIN + OIDC_AUDIENCE
"""

import pytest

# ---------------------------------------------------------------- none


def test_none_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_AUTH_MODE", "none")
    monkeypatch.setenv("RAGENT_ENV", "dev")

    from ragent.bootstrap.guard import enforce

    enforce()


def test_none_non_dev_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_AUTH_MODE", "none")
    monkeypatch.setenv("RAGENT_ENV", "production")

    from ragent.bootstrap.guard import enforce

    with pytest.raises(SystemExit):
        enforce()


# ---------------------------------------------------------------- user_header


def test_user_header_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_AUTH_MODE", "user_header")
    monkeypatch.setenv("RAGENT_ENV", "dev")

    from ragent.bootstrap.guard import enforce

    enforce()


def test_user_header_non_dev_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_AUTH_MODE", "user_header")
    monkeypatch.setenv("RAGENT_ENV", "production")

    from ragent.bootstrap.guard import enforce

    with pytest.raises(SystemExit):
        enforce()


def test_default_mode_is_user_header_and_boots_in_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RAGENT_AUTH_MODE", raising=False)
    monkeypatch.setenv("RAGENT_ENV", "dev")

    from ragent.bootstrap.guard import enforce

    enforce()


# ---------------------------------------------------------------- jwt_header


def test_jwt_header_happy_path_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_AUTH_MODE", "jwt_header")
    monkeypatch.setenv("RAGENT_ENV", "dev")
    monkeypatch.setenv("OIDC_DOMAIN", "idp.example.com")
    monkeypatch.setenv("OIDC_AUDIENCE", "ragent")

    from ragent.bootstrap.guard import enforce

    enforce()


def test_jwt_header_happy_path_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_AUTH_MODE", "jwt_header")
    monkeypatch.setenv("RAGENT_ENV", "production")
    monkeypatch.setenv("RAGENT_HOST", "0.0.0.0")
    monkeypatch.setenv("OIDC_DOMAIN", "idp.example.com")
    monkeypatch.setenv("OIDC_AUDIENCE", "ragent")

    from ragent.bootstrap.guard import enforce

    enforce()


def test_jwt_header_missing_oidc_domain_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_AUTH_MODE", "jwt_header")
    monkeypatch.setenv("RAGENT_ENV", "production")
    monkeypatch.delenv("OIDC_DOMAIN", raising=False)
    monkeypatch.setenv("OIDC_AUDIENCE", "ragent")

    from ragent.bootstrap.guard import enforce

    with pytest.raises(SystemExit):
        enforce()


def test_jwt_header_missing_oidc_audience_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_AUTH_MODE", "jwt_header")
    monkeypatch.setenv("RAGENT_ENV", "production")
    monkeypatch.setenv("OIDC_DOMAIN", "idp.example.com")
    monkeypatch.delenv("OIDC_AUDIENCE", raising=False)

    from ragent.bootstrap.guard import enforce

    with pytest.raises(SystemExit):
        enforce()


# ---------------------------------------------------------------- jwt_prefer_header


def test_jwt_prefer_header_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_AUTH_MODE", "jwt_prefer_header")
    monkeypatch.setenv("RAGENT_ENV", "dev")
    monkeypatch.setenv("OIDC_DOMAIN", "idp.example.com")
    monkeypatch.setenv("OIDC_AUDIENCE", "ragent")

    from ragent.bootstrap.guard import enforce

    enforce()


def test_jwt_prefer_header_non_dev_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_AUTH_MODE", "jwt_prefer_header")
    monkeypatch.setenv("RAGENT_ENV", "production")
    monkeypatch.setenv("OIDC_DOMAIN", "idp.example.com")
    monkeypatch.setenv("OIDC_AUDIENCE", "ragent")

    from ragent.bootstrap.guard import enforce

    with pytest.raises(SystemExit):
        enforce()


def test_jwt_prefer_header_missing_oidc_domain_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_AUTH_MODE", "jwt_prefer_header")
    monkeypatch.setenv("RAGENT_ENV", "dev")
    monkeypatch.delenv("OIDC_DOMAIN", raising=False)
    monkeypatch.setenv("OIDC_AUDIENCE", "ragent")

    from ragent.bootstrap.guard import enforce

    with pytest.raises(SystemExit):
        enforce()


# ---------------------------------------------------------------- misc


def test_invalid_auth_mode_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_AUTH_MODE", "bogus")
    monkeypatch.setenv("RAGENT_ENV", "dev")

    from ragent.bootstrap.guard import enforce

    with pytest.raises(SystemExit):
        enforce()


def test_invalid_log_level_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_AUTH_MODE", "user_header")
    monkeypatch.setenv("RAGENT_ENV", "dev")
    monkeypatch.setenv("LOG_LEVEL", "VERBOSE")

    from ragent.bootstrap.guard import enforce

    with pytest.raises(SystemExit):
        enforce()
