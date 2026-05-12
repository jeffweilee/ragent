"""Boot-time guard for RETRIEVAL_TOP_K (PR #63 codex P2 finding).

Spec §3.4.4 + §3.8.3 advertise `top_k` in [1, 200]. `DEFAULT_TOP_K`
backs the omitted-`top_k` path in both `/retrieve/v1` and the MCP
`tools/call retrieve` handler. An operator misconfiguring
`RETRIEVAL_TOP_K=500` would let MCP clients silently over-fetch past
the schema maximum on every default-args call. The guard refuses to
import `ragent.pipelines.chat` until the value is in range.
"""

from __future__ import annotations

import importlib
import sys

import pytest


@pytest.fixture
def _reload_chat(monkeypatch: pytest.MonkeyPatch):
    """Force re-import of `ragent.pipelines.chat` with a patched env."""

    def _reload(retrieval_top_k: str | None) -> None:
        if retrieval_top_k is None:
            monkeypatch.delenv("RETRIEVAL_TOP_K", raising=False)
        else:
            monkeypatch.setenv("RETRIEVAL_TOP_K", retrieval_top_k)
        sys.modules.pop("ragent.pipelines.chat", None)
        importlib.import_module("ragent.pipelines.chat")

    yield _reload
    # Restore the module to a sane state. monkeypatch's autocleanup reverts
    # the env var by now; we just need to drop the bad cached module so the
    # next import picks up the restored value. Re-import explicitly so any
    # downstream test in the same process sees a usable module.
    monkeypatch.delenv("RETRIEVAL_TOP_K", raising=False)
    sys.modules.pop("ragent.pipelines.chat", None)
    importlib.import_module("ragent.pipelines.chat")


def test_default_top_k_default_value_is_in_range(_reload_chat) -> None:
    """Unset → default of 20 → import succeeds."""
    _reload_chat(None)
    from ragent.pipelines.chat import DEFAULT_TOP_K, MAX_TOP_K

    assert DEFAULT_TOP_K == 20
    assert MAX_TOP_K == 200


def test_default_top_k_at_maximum_is_accepted(_reload_chat) -> None:
    """`RETRIEVAL_TOP_K=200` is the documented upper bound — must boot."""
    _reload_chat("200")
    from ragent.pipelines.chat import DEFAULT_TOP_K

    assert DEFAULT_TOP_K == 200


@pytest.mark.parametrize("bad_value", ["0", "201", "500", "-1"])
def test_default_top_k_out_of_range_refuses_to_import(_reload_chat, bad_value) -> None:
    """Out-of-range values raise at module import — operators see the misconfig
    on boot, not as a silent over-fetch on every MCP `tools/call` with omitted
    `top_k`."""
    with pytest.raises(RuntimeError, match="outside the advertised"):
        _reload_chat(bad_value)
