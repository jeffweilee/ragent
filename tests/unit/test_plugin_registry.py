"""T1.6 — PluginRegistry: register, fan_out, all_required_ok, duplicate raises, timeout."""

import asyncio
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, patch


@dataclass
class _OkPlugin:
    name: str
    required: bool
    queue: str = "extract.ok"
    calls: list[str] = field(default_factory=list)

    def extract(self, document_id: str) -> None:
        self.calls.append(document_id)

    def delete(self, document_id: str) -> None:  # noqa: ARG002
        pass

    def health(self) -> bool:
        return True


@dataclass
class _SlowPlugin:
    name: str = "slow"
    required: bool = True
    queue: str = "extract.slow"

    def extract(self, document_id: str) -> None:  # noqa: ARG002
        pass  # body not reached; asyncio.wait_for is mocked to raise TimeoutError

    def delete(self, document_id: str) -> None:  # noqa: ARG002
        pass

    def health(self) -> bool:
        return True


def test_register_and_fan_out_calls_all_plugins() -> None:
    from ragent.plugins.registry import PluginRegistry

    p1 = _OkPlugin(name="alpha", required=True)
    p2 = _OkPlugin(name="beta", required=False)
    reg = PluginRegistry()
    reg.register(p1)
    reg.register(p2)

    results = asyncio.run(reg.fan_out("doc_1"))

    assert p1.calls == ["doc_1"]
    assert p2.calls == ["doc_1"]
    assert len(results) == 2


def test_all_required_ok_true_when_all_required_succeed() -> None:
    from ragent.plugins.registry import PluginRegistry

    p = _OkPlugin(name="vec", required=True)
    reg = PluginRegistry()
    reg.register(p)
    results = asyncio.run(reg.fan_out("d"))
    assert reg.all_required_ok(results)


def test_all_required_ok_false_when_required_plugin_errors() -> None:
    from ragent.plugins.registry import PluginRegistry, Result

    p = _OkPlugin(name="vec", required=True)
    reg = PluginRegistry()
    reg.register(p)
    bad = [Result(plugin_name="vec", ok=False, error="boom")]
    assert not reg.all_required_ok(bad)


def test_all_required_ok_ignores_optional_errors() -> None:
    from ragent.plugins.registry import PluginRegistry, Result

    p_req = _OkPlugin(name="vec", required=True)
    p_opt = _OkPlugin(name="graph", required=False)
    reg = PluginRegistry()
    reg.register(p_req)
    reg.register(p_opt)
    results = [
        Result(plugin_name="vec", ok=True),
        Result(plugin_name="graph", ok=False, error="ignored"),
    ]
    assert reg.all_required_ok(results)


def test_duplicate_registration_raises() -> None:
    from ragent.plugins.registry import DuplicatePluginError, PluginRegistry

    reg = PluginRegistry()
    reg.register(_OkPlugin(name="vec", required=True))
    import pytest

    with pytest.raises(DuplicatePluginError):
        reg.register(_OkPlugin(name="vec", required=False))


def test_duplicate_does_not_overwrite_existing() -> None:
    import contextlib

    from ragent.plugins.registry import DuplicatePluginError, PluginRegistry

    p1 = _OkPlugin(name="vec", required=True)
    reg = PluginRegistry()
    reg.register(p1)
    with contextlib.suppress(DuplicatePluginError):
        reg.register(_OkPlugin(name="vec", required=False))
    # Original plugin is still registered unchanged
    results = asyncio.run(reg.fan_out("d"))
    assert any(r.plugin_name == "vec" and r.ok for r in results)


def test_fan_out_timeout_returns_timeout_result() -> None:
    from ragent.plugins.registry import PluginRegistry

    slow = _SlowPlugin()
    reg = PluginRegistry()
    reg.register(slow)

    # Mock wait_for to raise TimeoutError so no real threads block process exit.
    timeout_mock = AsyncMock(side_effect=TimeoutError)
    with patch("ragent.plugins.registry.asyncio.wait_for", timeout_mock):
        results = asyncio.run(reg.fan_out("doc_x"))

    assert len(results) == 1
    assert results[0].error == "timeout"
    assert not results[0].ok


def test_timeout_on_required_plugin_fails_all_required_ok() -> None:
    from ragent.plugins.registry import PluginRegistry

    slow = _SlowPlugin(name="slow_req", required=True)
    reg = PluginRegistry()
    reg.register(slow)

    timeout_mock = AsyncMock(side_effect=TimeoutError)
    with patch("ragent.plugins.registry.asyncio.wait_for", timeout_mock):
        results = asyncio.run(reg.fan_out("d"))
    assert not reg.all_required_ok(results)


def test_result_ok_defaults_to_true() -> None:
    from ragent.plugins.registry import Result

    r = Result(plugin_name="x")
    assert r.ok is True
    assert r.error is None
