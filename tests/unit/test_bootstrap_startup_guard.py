"""T7.6 — Startup guard: reject non-dev/non-open configs, enforce loopback host (B28)."""

import pytest


def test_happy_path_dev_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_ENV", "dev")
    monkeypatch.setenv("RAGENT_AUTH_DISABLED", "true")
    monkeypatch.setenv("RAGENT_HOST", "127.0.0.1")
    monkeypatch.setenv("RAGENT_PORT", "8000")
    monkeypatch.setenv("LOG_LEVEL", "INFO")

    from ragent.bootstrap.guard import enforce

    enforce()  # must not raise


def test_missing_auth_disabled_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_ENV", "dev")
    monkeypatch.delenv("RAGENT_AUTH_DISABLED", raising=False)

    from ragent.bootstrap.guard import enforce

    with pytest.raises(SystemExit):
        enforce()


def test_auth_not_true_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_ENV", "dev")
    monkeypatch.setenv("RAGENT_AUTH_DISABLED", "false")

    from ragent.bootstrap.guard import enforce

    with pytest.raises(SystemExit):
        enforce()


def test_non_dev_env_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_ENV", "production")
    monkeypatch.setenv("RAGENT_AUTH_DISABLED", "true")

    from ragent.bootstrap.guard import enforce

    with pytest.raises(SystemExit):
        enforce()


def test_non_loopback_host_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_ENV", "dev")
    monkeypatch.setenv("RAGENT_AUTH_DISABLED", "true")
    monkeypatch.setenv("RAGENT_HOST", "0.0.0.0")

    from ragent.bootstrap.guard import enforce

    with pytest.raises(SystemExit):
        enforce()


def test_invalid_log_level_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_ENV", "dev")
    monkeypatch.setenv("RAGENT_AUTH_DISABLED", "true")
    monkeypatch.setenv("RAGENT_HOST", "127.0.0.1")
    monkeypatch.setenv("LOG_LEVEL", "VERBOSE")

    from ragent.bootstrap.guard import enforce

    with pytest.raises(SystemExit):
        enforce()


def test_default_host_is_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGENT_ENV", "dev")
    monkeypatch.setenv("RAGENT_AUTH_DISABLED", "true")
    monkeypatch.delenv("RAGENT_HOST", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)

    from ragent.bootstrap.guard import enforce

    enforce()  # default host 127.0.0.1 should pass
