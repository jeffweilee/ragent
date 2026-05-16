"""One bad file / tool must not break the rest of the Hub."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from ragent.mcp_hub.mcp_hub import build_hub


def _write(path: Path, body: str) -> Path:
    path.write_text(dedent(body).strip())
    return path


@pytest.mark.asyncio
async def test_bad_yaml_syntax_skips_only_that_system(tmp_path: Path):
    d = tmp_path / "tools.d"
    d.mkdir()
    _write(
        d / "ok.yaml",
        """
        tools:
          - name: ping
            method: GET
            path: https://ok.example.com/ping
        """,
    )
    (d / "broken.yaml").write_text("this is: not: valid: yaml:\n  - [unbalanced")
    bundle = build_hub(d, name="t")

    tool_names = sorted(t.name for t in (await bundle.hub.list_tools()))
    assert "ok.ping" in tool_names
    assert not any(n.startswith("broken.") for n in tool_names)
    assert any("broken.yaml" in f.source for f in bundle.failures)

    for c in bundle.clients.values():
        await c.aclose()


@pytest.mark.asyncio
async def test_bad_tool_schema_skips_only_that_tool(tmp_path: Path):
    d = tmp_path / "tools.d"
    d.mkdir()
    _write(
        d / "identity.yaml",
        """
        tools:
          - name: good
            method: GET
            path: https://identity.example.com/good
          - name: bad
            method: GET
            path: https://identity.example.com/bad
            parameters:
              - name: x
                type: bigint
                location: query
        """,
    )
    bundle = build_hub(d, name="t")

    tool_names = sorted(t.name for t in (await bundle.hub.list_tools()))
    assert "identity.good" in tool_names
    assert "identity.bad" not in tool_names
    assert any("identity.yaml" in f.source and "bad" in f.source for f in bundle.failures)

    for c in bundle.clients.values():
        await c.aclose()


@pytest.mark.asyncio
async def test_all_systems_broken_hub_still_starts_with_zero_tools(tmp_path: Path):
    d = tmp_path / "tools.d"
    d.mkdir()
    (d / "broken1.yaml").write_text(": :")
    (d / "broken2.yaml").write_text("[[[")
    bundle = build_hub(d, name="t")

    tools = await bundle.hub.list_tools()
    assert tools == []
    assert len(bundle.failures) >= 2

    for c in bundle.clients.values():
        await c.aclose()


def test_strict_mode_raises_on_any_failure(tmp_path: Path):
    """Doctor uses strict=True; runtime build_hub uses strict=False."""
    from ragent.mcp_hub.mcp_hub import load_tools_yaml

    d = tmp_path / "tools.d"
    d.mkdir()
    _write(
        d / "broken.yaml",
        """
        tools:
          - name: bad
            method: GET
            path: /bad
            parameters:
              - name: x
                type: bigint
                location: query
        """,
    )
    with pytest.raises(ValueError):
        load_tools_yaml(d, strict=True)


def test_non_strict_mode_collects_failures(tmp_path: Path):
    from ragent.mcp_hub.mcp_hub import load_tools_yaml

    d = tmp_path / "tools.d"
    d.mkdir()
    _write(
        d / "mixed.yaml",
        """
        tools:
          - name: good
            method: GET
            path: https://api.example.com/good
          - name: bad
            method: GET
            path: /bad
            parameters:
              - name: x
                type: bigint
                location: query
        """,
    )
    result = load_tools_yaml(d, strict=False)
    assert len(result.tools) == 1
    assert result.tools[0].name == "mixed.good"
    assert len(result.failures) == 1
    assert "bad" in result.failures[0].source
