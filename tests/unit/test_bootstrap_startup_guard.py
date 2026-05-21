"""Startup guard: validates auth-mode coherence and safety constraints.

Three modes supported (mirrors `src/ragent/bootstrap/composition.py` lines
311-313 where the JWT verifier is conditionally built):

  A. RAGENT_AUTH_DISABLED=true                                — open auth
  B. AUTH_DISABLED=false, TRUST_X_USER_ID_HEADER=true         — JWT bypassed
  C. AUTH_DISABLED=false, TRUST_X_USER_ID_HEADER=false        — OIDC JWT

Modes A and B trust an unverified header; they are dev-only. Mode A
additionally requires loopback bind because the endpoint has no auth
surface at all.
"""

import pytest

# ---------------------------------------------------------------- Mode A


def test_mode_a_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_ENV", "dev")
    monkeypatch.setenv("RAGENT_AUTH_DISABLED", "true")
    monkeypatch.setenv("RAGENT_HOST", "127.0.0.1")
    monkeypatch.setenv("LOG_LEVEL", "INFO")

    from ragent.bootstrap.guard import enforce

    enforce()  # must not raise


def test_mode_a_non_dev_env_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_ENV", "production")
    monkeypatch.setenv("RAGENT_AUTH_DISABLED", "true")

    from ragent.bootstrap.guard import enforce

    with pytest.raises(SystemExit):
        enforce()


def test_mode_a_non_loopback_host_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_ENV", "dev")
    monkeypatch.setenv("RAGENT_AUTH_DISABLED", "true")
    monkeypatch.setenv("RAGENT_HOST", "0.0.0.0")

    from ragent.bootstrap.guard import enforce

    with pytest.raises(SystemExit):
        enforce()


def test_mode_a_default_host_is_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_ENV", "dev")
    monkeypatch.setenv("RAGENT_AUTH_DISABLED", "true")
    monkeypatch.delenv("RAGENT_HOST", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)

    from ragent.bootstrap.guard import enforce

    enforce()


# ---------------------------------------------------------------- Mode B


def test_mode_b_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_ENV", "dev")
    monkeypatch.setenv("RAGENT_AUTH_DISABLED", "false")
    monkeypatch.setenv("RAGENT_TRUST_X_USER_ID_HEADER", "true")

    from ragent.bootstrap.guard import enforce

    enforce()  # must not raise — JWT bypassed, X-User-Id trusted


def test_mode_b_non_dev_env_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_ENV", "production")
    monkeypatch.setenv("RAGENT_AUTH_DISABLED", "false")
    monkeypatch.setenv("RAGENT_TRUST_X_USER_ID_HEADER", "true")

    from ragent.bootstrap.guard import enforce

    with pytest.raises(SystemExit):
        enforce()


# ---------------------------------------------------------------- Mode C


def test_mode_c_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_ENV", "production")
    monkeypatch.setenv("RAGENT_AUTH_DISABLED", "false")
    monkeypatch.delenv("RAGENT_TRUST_X_USER_ID_HEADER", raising=False)
    monkeypatch.setenv("RAGENT_HOST", "0.0.0.0")
    monkeypatch.setenv("OIDC_DOMAIN", "idp.example.com")
    monkeypatch.setenv("OIDC_AUDIENCE", "ragent")

    from ragent.bootstrap.guard import enforce

    enforce()  # OIDC mode tolerates non-dev env and non-loopback host


def test_mode_c_missing_oidc_domain_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_ENV", "dev")
    monkeypatch.setenv("RAGENT_AUTH_DISABLED", "false")
    monkeypatch.delenv("RAGENT_TRUST_X_USER_ID_HEADER", raising=False)
    monkeypatch.delenv("OIDC_DOMAIN", raising=False)
    monkeypatch.setenv("OIDC_AUDIENCE", "ragent")

    from ragent.bootstrap.guard import enforce

    with pytest.raises(SystemExit):
        enforce()


def test_mode_c_missing_oidc_audience_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_ENV", "dev")
    monkeypatch.setenv("RAGENT_AUTH_DISABLED", "false")
    monkeypatch.delenv("RAGENT_TRUST_X_USER_ID_HEADER", raising=False)
    monkeypatch.setenv("OIDC_DOMAIN", "idp.example.com")
    monkeypatch.delenv("OIDC_AUDIENCE", raising=False)

    from ragent.bootstrap.guard import enforce

    with pytest.raises(SystemExit):
        enforce()


def test_mode_c_default_auth_is_oidc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_ENV", "dev")
    monkeypatch.delenv("RAGENT_AUTH_DISABLED", raising=False)
    monkeypatch.delenv("RAGENT_TRUST_X_USER_ID_HEADER", raising=False)
    monkeypatch.delenv("OIDC_DOMAIN", raising=False)

    from ragent.bootstrap.guard import enforce

    with pytest.raises(SystemExit):
        enforce()  # default mode is OIDC; missing OIDC_DOMAIN exits


# ---------------------------------------------------------------- misc


def test_invalid_log_level_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_ENV", "dev")
    monkeypatch.setenv("RAGENT_AUTH_DISABLED", "true")
    monkeypatch.setenv("RAGENT_HOST", "127.0.0.1")
    monkeypatch.setenv("LOG_LEVEL", "VERBOSE")

    from ragent.bootstrap.guard import enforce

    with pytest.raises(SystemExit):
        enforce()
