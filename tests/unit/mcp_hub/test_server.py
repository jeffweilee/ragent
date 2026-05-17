"""Server entry-point env-var validation."""

from __future__ import annotations

import pytest

from ragent.mcp_hub.server import main


def test_non_numeric_port_exits_with_clear_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setenv("MCP_HUB_PORT", "not-a-number")
    monkeypatch.setenv("MCP_HUB_TOOLS_YAML", "/nonexistent.yaml")

    with pytest.raises(SystemExit) as ex:
        main()

    assert "MCP_HUB_PORT" in str(ex.value)
