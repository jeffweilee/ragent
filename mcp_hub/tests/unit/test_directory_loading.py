"""Directory-mode loading: each *.yaml in the directory is one system.
Tool names are auto-qualified as `<system>.<tool>`."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from mcp_hub.mcp_hub import load_tools_yaml


def _write(path: Path, body: str) -> Path:
    path.write_text(dedent(body).strip())
    return path


def test_single_file_still_works(tmp_path: Path):
    """Regression: passing a file path preserves the old single-system behaviour."""
    yml = _write(
        tmp_path / "tools.yaml",
        """
        defaults:
          base_url: https://api.example.com
        tools:
          - name: ping
            method: GET
            path: /ping
        """,
    )
    tools = load_tools_yaml(yml).tools
    assert len(tools) == 1
    assert tools[0].name == "tools.ping"


def test_directory_loads_every_yaml(tmp_path: Path):
    d = tmp_path / "tools.d"
    d.mkdir()
    _write(
        d / "identity.yaml",
        """
        defaults:
          base_url: https://identity.example.com
        tools:
          - name: get_profile
            method: GET
            path: /me
        """,
    )
    _write(
        d / "billing.yaml",
        """
        defaults:
          base_url: https://billing.example.com
        tools:
          - name: list_charges
            method: GET
            path: /charges
        """,
    )
    tools = load_tools_yaml(d).tools
    names = sorted(t.name for t in tools)
    assert names == ["billing.list_charges", "identity.get_profile"]


def test_explicit_system_overrides_filename(tmp_path: Path):
    d = tmp_path / "tools.d"
    d.mkdir()
    _write(
        d / "anything.yaml",
        """
        system: identity-v2
        tools:
          - name: get_profile
            method: GET
            path: https://identity.example.com/me
        """,
    )
    tools = load_tools_yaml(d).tools
    assert tools[0].name == "identity-v2.get_profile"


def test_cross_system_same_tool_name_is_allowed(tmp_path: Path):
    d = tmp_path / "tools.d"
    d.mkdir()
    _write(
        d / "identity.yaml",
        """
        tools:
          - name: get_profile
            method: GET
            path: https://identity.example.com/me
        """,
    )
    _write(
        d / "billing.yaml",
        """
        tools:
          - name: get_profile
            method: GET
            path: https://billing.example.com/customer
        """,
    )
    tools = load_tools_yaml(d).tools
    names = sorted(t.name for t in tools)
    assert names == ["billing.get_profile", "identity.get_profile"]


def test_within_system_duplicate_name_is_a_failure(tmp_path: Path):
    d = tmp_path / "tools.d"
    d.mkdir()
    _write(
        d / "identity.yaml",
        """
        tools:
          - name: get_profile
            method: GET
            path: https://identity.example.com/me
          - name: get_profile
            method: GET
            path: https://identity.example.com/profile
        """,
    )
    with pytest.raises(ValueError, match="duplicate"):
        load_tools_yaml(d, strict=True)


def test_empty_directory_returns_no_tools(tmp_path: Path):
    d = tmp_path / "tools.d"
    d.mkdir()
    tools = load_tools_yaml(d).tools
    assert tools == []


def _bundled_dir() -> Path:
    """Resolve tools.example.d via the package, not by walking up the test file."""
    from importlib.resources import files

    return Path(str(files("mcp_hub") / "tools.example.d"))


def test_bundled_ragent_yaml_exposes_retrieve_tool():
    result = load_tools_yaml(_bundled_dir())
    by_name = {t.name: t for t in result.tools}
    assert "ragent.retrieve" in by_name

    tool = by_name["ragent.retrieve"]
    assert tool.method == "POST"
    assert tool.path == "/retrieve/v1"
    assert tool.forward_headers.get("X-User-Id") == "{x-user-id}"


def test_bundled_httpbin_yaml_exposes_two_tools():
    result = load_tools_yaml(_bundled_dir())
    names = {t.name for t in result.tools}
    assert "httpbin.get_ip" in names
    assert "httpbin.post_json" in names
