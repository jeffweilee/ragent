"""Coverage for ragent.utility.env typed accessors."""

from __future__ import annotations

import pytest

from ragent.utility.env import bool_env, float_env, int_env, optional_float_env, require


def test_require_returns_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X", "v")
    assert require("X") == "v"


def test_require_exits_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("X", raising=False)
    with pytest.raises(SystemExit):
        require("X")


def test_int_env_default_and_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("N", raising=False)
    assert int_env("N", 42) == 42
    monkeypatch.setenv("N", "7")
    assert int_env("N", 42) == 7


def test_int_env_exits_on_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("N", "not-an-int")
    with pytest.raises(SystemExit):
        int_env("N", 0)


def test_bool_env_truthy_strings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("B", raising=False)
    assert bool_env("B", default=True) is True
    for v in ("1", "true", "yes", "TRUE", "Yes"):
        monkeypatch.setenv("B", v)
        assert bool_env("B", default=False) is True
    monkeypatch.setenv("B", "no")
    assert bool_env("B", default=True) is False


def test_float_env_default_parse_and_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("F", raising=False)
    assert float_env("F", 1.5) == 1.5
    monkeypatch.setenv("F", "2.25")
    assert float_env("F", 0.0) == 2.25
    monkeypatch.setenv("F", "nope")
    with pytest.raises(SystemExit):
        float_env("F", 0.0)


def test_optional_float_env_returns_none_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPT_F", raising=False)
    assert optional_float_env("OPT_F") is None


def test_optional_float_env_parses_float_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPT_F", "0.75")
    assert optional_float_env("OPT_F") == pytest.approx(0.75)


def test_optional_float_env_returns_none_when_empty_string(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPT_F", "")
    assert optional_float_env("OPT_F") is None


def test_optional_float_env_exits_on_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPT_F", "not-a-float")
    with pytest.raises(SystemExit):
        optional_float_env("OPT_F")
