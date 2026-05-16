"""tools.yaml validator — fails CI if the registry is malformed."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from ragent.mcp_hub.doctor import check_yaml
from ragent.mcp_hub.doctor import main as doctor_main


def _write(path: Path, body: str) -> Path:
    path.write_text(dedent(body).strip())
    return path


def test_valid_yaml_returns_no_errors(tmp_path: Path):
    yml = _write(
        tmp_path / "tools.yaml",
        """
        defaults:
          base_url: https://api.example.com
        tools:
          - name: get_user
            method: GET
            path: /users/{user_id}
            parameters:
              - name: user_id
                type: integer
                location: path
                required: true
        """,
    )
    assert check_yaml(yml) == ([], 1)


def test_missing_file_is_reported(tmp_path: Path):
    errors, _ = check_yaml(tmp_path / "nope.yaml")
    assert len(errors) == 1
    assert "failed to load" in errors[0]


def test_duplicate_tool_names_flagged(tmp_path: Path):
    yml = _write(
        tmp_path / "tools.yaml",
        """
        tools:
          - name: dup
            method: GET
            path: /a
          - name: dup
            method: GET
            path: /b
        """,
    )
    errors, _ = check_yaml(yml)
    assert any("duplicate" in e for e in errors)


def test_path_placeholder_without_param_flagged(tmp_path: Path):
    yml = _write(
        tmp_path / "tools.yaml",
        """
        tools:
          - name: bad
            method: GET
            path: /users/{user_id}/orders/{order_id}
            parameters:
              - name: user_id
                type: integer
                location: path
                required: true
        """,
    )
    errors, _ = check_yaml(yml)
    assert any("order_id" in e and "no matching parameter" in e for e in errors)


def test_path_param_not_in_template_flagged(tmp_path: Path):
    yml = _write(
        tmp_path / "tools.yaml",
        """
        tools:
          - name: bad
            method: GET
            path: /users
            parameters:
              - name: user_id
                type: integer
                location: path
                required: true
        """,
    )
    errors, _ = check_yaml(yml)
    assert any("user_id" in e and "not used in path" in e for e in errors)


def test_body_param_on_get_flagged(tmp_path: Path):
    yml = _write(
        tmp_path / "tools.yaml",
        """
        tools:
          - name: bad
            method: GET
            path: /search
            parameters:
              - name: payload
                type: object
                location: body
                required: true
        """,
    )
    errors, _ = check_yaml(yml)
    assert any("body parameters" in e and "GET" in e for e in errors)


def test_unsupported_type_flagged(tmp_path: Path):
    yml = _write(
        tmp_path / "tools.yaml",
        """
        tools:
          - name: bad
            method: GET
            path: /x
            parameters:
              - name: y
                type: bigint
                location: query
        """,
    )
    errors, _ = check_yaml(yml)
    assert any("unsupported type" in e for e in errors)


def test_missing_base_url_with_relative_path_flagged(tmp_path: Path):
    yml = _write(
        tmp_path / "tools.yaml",
        """
        tools:
          - name: needs_base
            method: GET
            path: /users
        """,
    )
    errors, _ = check_yaml(yml)
    assert any("base_url" in e for e in errors)


def test_missing_default_base_url_with_per_tool_override_is_ok(tmp_path: Path):
    yml = _write(
        tmp_path / "tools.yaml",
        """
        tools:
          - name: scoped
            base_url: https://api-b.example.com
            method: GET
            path: /me
        """,
    )
    errors, _ = check_yaml(yml)
    assert errors == []


def test_missing_base_url_with_absolute_path_is_ok(tmp_path: Path):
    """Absolute tool paths don't need a base_url."""
    yml = _write(
        tmp_path / "tools.yaml",
        """
        tools:
          - name: absolute
            method: GET
            path: https://other.example.com/users
        """,
    )
    errors, _ = check_yaml(yml)
    assert errors == []


def test_cli_exits_zero_on_valid(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
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
    rc = doctor_main([str(yml)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "OK" in out


def test_cli_exits_nonzero_on_invalid(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    yml = _write(
        tmp_path / "tools.yaml",
        """
        tools:
          - name: dup
            method: GET
            path: /a
          - name: dup
            method: GET
            path: /b
        """,
    )
    rc = doctor_main([str(yml)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "duplicate" in err
