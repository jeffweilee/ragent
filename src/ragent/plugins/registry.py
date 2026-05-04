"""PluginRegistry with concurrent fan_out and per-plugin timeout (spec §3.3, S11, S29)."""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ragent.plugins.protocol import ExtractorPlugin


@dataclass
class Result:
    plugin_name: str
    ok: bool = True
    error: str | None = None


class DuplicatePluginError(Exception):
    pass


class PluginRegistry:
    def __init__(self) -> None:
        self._plugins: dict[str, ExtractorPlugin] = {}
        self._timeout = float(os.environ.get("PLUGIN_FAN_OUT_TIMEOUT_SECONDS", "60"))

    def register(self, plugin: ExtractorPlugin) -> None:
        if plugin.name in self._plugins:
            raise DuplicatePluginError(f"Plugin '{plugin.name}' is already registered")
        self._plugins[plugin.name] = plugin

    async def fan_out(self, document_id: str) -> list[Result]:
        pairs = [(p.name, p.extract) for p in self._plugins.values()]
        return await _fan_out_callables(pairs, document_id, self._timeout)

    async def fan_out_delete(self, document_id: str) -> list[Result]:
        pairs = [(p.name, p.delete) for p in self._plugins.values()]
        return await _fan_out_callables(pairs, document_id, self._timeout)

    def all_required_ok(self, results: list[Result]) -> bool:
        required = {name for name, p in self._plugins.items() if p.required}
        return all(r.ok for r in results if r.plugin_name in required)


async def _fan_out_callables(
    pairs: list[tuple[str, Callable[[str], None]]],
    document_id: str,
    timeout: float,
) -> list[Result]:
    # Dedicated executor: asyncio.run() only joins the default executor, so timed-out
    # threads from this executor don't block process exit.
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=len(pairs) or 1)
    loop = asyncio.get_running_loop()
    tasks = [
        asyncio.wait_for(loop.run_in_executor(executor, fn, document_id), timeout=timeout)
        for _, fn in pairs
    ]
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    executor.shutdown(wait=False, cancel_futures=True)

    results = []
    for (name, _), outcome in zip(pairs, outcomes, strict=True):
        if isinstance(outcome, TimeoutError):
            results.append(Result(plugin_name=name, ok=False, error="timeout"))
        elif isinstance(outcome, Exception):
            results.append(Result(plugin_name=name, ok=False, error=str(outcome)))
        else:
            results.append(Result(plugin_name=name))
    return results
