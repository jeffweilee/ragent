"""T-RR.7 (B38) — _check_infra_ready pre-warms every TokenManager.

A wrong `AI_API_AUTH_URL` or stale `AI_*_J1_TOKEN` must surface at boot
(lifespan abort), not at first /chat or /ingest dispatch. The probe
invokes `tm.get_token()` for each entry in `container.token_managers`;
a single failure raises RuntimeError and short-circuits boot.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_broker_with_tasks() -> Any:  # type: ignore[name-defined]
    broker = MagicMock()
    broker.find_task.return_value = object()  # any non-None registered handle
    return broker


from typing import Any  # noqa: E402


async def _run(container: Any) -> None:
    from ragent.bootstrap.app import _check_infra_ready

    ok_probes = {
        "mariadb": AsyncMock(return_value=None),
        "es": AsyncMock(return_value=None),
    }
    await _check_infra_ready(ok_probes, _make_broker_with_tasks(), container)


@pytest.mark.anyio("asyncio")
async def test_check_infra_ready_invokes_get_token_for_each_token_manager() -> None:
    tm_llm = MagicMock()
    tm_emb = MagicMock()
    tm_rerank = MagicMock()
    container = SimpleNamespace(token_managers=(tm_llm, tm_emb, tm_rerank))

    await _run(container)

    tm_llm.get_token.assert_called_once()
    tm_emb.get_token.assert_called_once()
    tm_rerank.get_token.assert_called_once()


@pytest.mark.anyio("asyncio")
async def test_check_infra_ready_skips_none_token_managers() -> None:
    """When CHAT_RERANK_ENABLED=false, rerank_tm is None — must not crash."""
    tm_llm = MagicMock()
    tm_emb = MagicMock()
    container = SimpleNamespace(token_managers=(tm_llm, tm_emb, None))

    await _run(container)

    tm_llm.get_token.assert_called_once()
    tm_emb.get_token.assert_called_once()


@pytest.mark.anyio("asyncio")
async def test_check_infra_ready_raises_when_token_exchange_fails() -> None:
    tm_good = MagicMock()
    tm_bad = MagicMock()
    tm_bad.get_token.side_effect = RuntimeError("J1→J2 exchange refused: 401")
    container = SimpleNamespace(token_managers=(tm_good, tm_bad, None))

    with pytest.raises(RuntimeError, match="token"):
        await _run(container)
