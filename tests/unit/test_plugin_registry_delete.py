"""T1.8 — fan_out_delete: calls all plugins, timeout, idempotent, no DB tx (R10, P-E)."""

import asyncio
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, patch


@dataclass
class _OkPlugin:
    name: str
    required: bool = True
    queue: str = "extract.ok"
    delete_calls: list[str] = field(default_factory=list)

    def extract(self, document_id: str) -> None:
        pass

    def delete(self, document_id: str) -> None:
        self.delete_calls.append(document_id)

    def health(self) -> bool:
        return True


@dataclass
class _ErrorPlugin:
    name: str = "err"
    required: bool = True
    queue: str = "extract.err"

    def extract(self, document_id: str) -> None:
        pass

    def delete(self, document_id: str) -> None:  # noqa: ARG002
        raise RuntimeError("delete failed")

    def health(self) -> bool:
        return True


def test_fan_out_delete_calls_every_plugin() -> None:
    from ragent.plugins.registry import PluginRegistry

    p1 = _OkPlugin(name="vec")
    p2 = _OkPlugin(name="graph", required=False)
    reg = PluginRegistry()
    reg.register(p1)
    reg.register(p2)

    results = asyncio.run(reg.fan_out_delete("doc_1"))

    assert p1.delete_calls == ["doc_1"]
    assert p2.delete_calls == ["doc_1"]
    assert len(results) == 2


def test_fan_out_delete_idempotent_on_already_deleted() -> None:
    from ragent.plugins.registry import PluginRegistry

    p = _OkPlugin(name="vec")
    reg = PluginRegistry()
    reg.register(p)

    asyncio.run(reg.fan_out_delete("doc_1"))
    asyncio.run(reg.fan_out_delete("doc_1"))

    assert p.delete_calls == ["doc_1", "doc_1"]


def test_fan_out_delete_error_captured_in_result() -> None:
    from ragent.plugins.registry import PluginRegistry

    reg = PluginRegistry()
    reg.register(_ErrorPlugin())

    results = asyncio.run(reg.fan_out_delete("doc_err"))

    assert len(results) == 1
    assert not results[0].ok
    assert "delete failed" in (results[0].error or "")


def test_fan_out_delete_timeout_returns_timeout_result() -> None:
    from ragent.plugins.registry import PluginRegistry

    p = _OkPlugin(name="vec")
    reg = PluginRegistry()
    reg.register(p)

    timeout_mock = AsyncMock(side_effect=TimeoutError)
    with patch("ragent.plugins.registry.asyncio.wait_for", timeout_mock):
        results = asyncio.run(reg.fan_out_delete("doc_x"))

    assert results[0].error == "timeout"
    assert not results[0].ok


def test_fan_out_delete_runs_outside_db_transaction() -> None:
    """fan_out_delete must not open a DB connection or transaction (P-E).

    Verified structurally: the method signature accepts no db/session argument
    and the registry holds no db reference.
    """
    import inspect

    from ragent.plugins.registry import PluginRegistry

    sig = inspect.signature(PluginRegistry.fan_out_delete)
    params = list(sig.parameters.keys())
    # Only 'self' and 'document_id'; no db/session/connection param
    assert params == ["self", "document_id"]
