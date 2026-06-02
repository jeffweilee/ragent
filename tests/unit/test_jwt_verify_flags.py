"""T-AM.3 — RAGENT_JWT_VERIFY_AUD / RAGENT_JWT_VERIFY_EXP flag tests."""

from __future__ import annotations

import time

import pytest

# ---------------------------------------------------------------- guard enforcement


def test_verify_aud_false_requires_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_AUTH_MODE", "jwt_header")
    monkeypatch.setenv("RAGENT_ENV", "production")
    monkeypatch.setenv("OIDC_DOMAIN", "idp.example.com")
    monkeypatch.setenv("OIDC_AUDIENCE", "ragent")
    monkeypatch.setenv("RAGENT_JWT_VERIFY_AUD", "false")
    monkeypatch.delenv("RAGENT_JWT_VERIFY_EXP", raising=False)

    from ragent.bootstrap.guard import enforce

    with pytest.raises(SystemExit):
        enforce()


def test_verify_exp_false_requires_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_AUTH_MODE", "jwt_header")
    monkeypatch.setenv("RAGENT_ENV", "production")
    monkeypatch.setenv("OIDC_DOMAIN", "idp.example.com")
    monkeypatch.setenv("OIDC_AUDIENCE", "ragent")
    monkeypatch.delenv("RAGENT_JWT_VERIFY_AUD", raising=False)
    monkeypatch.setenv("RAGENT_JWT_VERIFY_EXP", "false")

    from ragent.bootstrap.guard import enforce

    with pytest.raises(SystemExit):
        enforce()


def test_verify_flags_false_allowed_in_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_AUTH_MODE", "jwt_header")
    monkeypatch.setenv("RAGENT_ENV", "dev")
    monkeypatch.setenv("OIDC_DOMAIN", "idp.example.com")
    monkeypatch.setenv("OIDC_AUDIENCE", "ragent")
    monkeypatch.setenv("RAGENT_JWT_VERIFY_AUD", "false")
    monkeypatch.setenv("RAGENT_JWT_VERIFY_EXP", "false")

    from ragent.bootstrap.guard import enforce

    enforce()  # must not exit


def test_verify_flags_default_true_allowed_in_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_AUTH_MODE", "jwt_header")
    monkeypatch.setenv("RAGENT_ENV", "production")
    monkeypatch.setenv("OIDC_DOMAIN", "idp.example.com")
    monkeypatch.setenv("OIDC_AUDIENCE", "ragent")
    monkeypatch.delenv("RAGENT_JWT_VERIFY_AUD", raising=False)
    monkeypatch.delenv("RAGENT_JWT_VERIFY_EXP", raising=False)

    from ragent.bootstrap.guard import enforce

    enforce()


# ---------------------------------------------------------------- build_token_manager flags


def test_token_manager_stores_verify_flags(oidc_token_manager_factory) -> None:
    tm = oidc_token_manager_factory(verify_aud=False, verify_exp=True)
    assert tm.verify_aud is False
    assert tm.verify_exp is True


# ---------------------------------------------------------------- verify_jwt: skip aud


def test_verify_jwt_skip_aud_accepts_wrong_audience(oidc_token_manager_factory, make_token) -> None:
    tm = oidc_token_manager_factory(verify_aud=False)
    token = make_token(preferred_username="alice", aud="wrong-audience")

    from ragent.auth.jwt import verify_jwt

    user_id = verify_jwt(token, claim_user_id="preferred_username", token_manager=tm)
    assert user_id == "alice"


def test_verify_jwt_with_aud_rejects_wrong_audience(oidc_token_manager_factory, make_token) -> None:
    tm = oidc_token_manager_factory(verify_aud=True)
    token = make_token(preferred_username="alice", aud="wrong-audience")

    from ragent.auth.jwt import JwtAuthError, verify_jwt

    with pytest.raises(JwtAuthError):
        verify_jwt(token, claim_user_id="preferred_username", token_manager=tm)


# ---------------------------------------------------------------- verify_jwt: skip exp


def test_verify_jwt_skip_exp_accepts_expired_token(oidc_token_manager_factory, make_token) -> None:
    tm = oidc_token_manager_factory(verify_exp=False)
    token = make_token(preferred_username="alice", exp=int(time.time()) - 3600)

    from ragent.auth.jwt import verify_jwt

    user_id = verify_jwt(token, claim_user_id="preferred_username", token_manager=tm)
    assert user_id == "alice"


def test_verify_jwt_with_exp_rejects_expired_token(oidc_token_manager_factory, make_token) -> None:
    tm = oidc_token_manager_factory(verify_exp=True)
    token = make_token(preferred_username="alice", exp=int(time.time()) - 1)

    from ragent.auth.jwt import JwtAuthError, verify_jwt

    with pytest.raises(JwtAuthError):
        verify_jwt(token, claim_user_id="preferred_username", token_manager=tm)
