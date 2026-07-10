"""tools.yaml validator — fails CI if the registry is malformed."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from mcp_hub.doctor import check_yaml
from mcp_hub.doctor import main as doctor_main


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
    assert "does not exist" in errors[0]


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


def test_placeholder_ok_accepts_valid_secret_syntax(tmp_path: Path):
    """--placeholder-ok mode accepts valid {% .Secrets.KEY %} syntax without
    resolving env vars."""
    yml = _write(
        tmp_path / "tools.yaml",
        """
        tools:
          - name: secure
            method: GET
            path: https://api.example.com/x
            static_headers:
              X-Api-Token: "{% .Secrets.API_TOKEN %}"
        """,
    )
    errors, count = check_yaml(yml, placeholder_ok=True)
    assert errors == []
    assert count == 1


def test_placeholder_ok_rejects_double_brace_helm_syntax(tmp_path: Path):
    """{{ }} would conflict with Helm templating — doctor must flag it."""
    yml = _write(
        tmp_path / "tools.yaml",
        """
        tools:
          - name: bad
            method: GET
            path: https://api.example.com/x
            static_headers:
              X-Api-Token: "{{ .Values.token }}"
        """,
    )
    errors, _ = check_yaml(yml, placeholder_ok=True)
    assert any("{{" in e for e in errors)


def test_placeholder_ok_rejects_invalid_key_format(tmp_path: Path):
    """Key must match [A-Z][A-Z0-9_]* — lowercase is rejected."""
    yml = _write(
        tmp_path / "tools.yaml",
        """
        tools:
          - name: bad
            method: GET
            path: https://api.example.com/x
            static_headers:
              X-Api-Token: "{% .Secrets.lowercase_key %}"
        """,
    )
    errors, _ = check_yaml(yml, placeholder_ok=True)
    assert any("placeholder" in e.lower() or "secret" in e.lower() for e in errors)


def test_invalid_param_name_flagged(tmp_path: Path):
    """Parameter names that aren't valid Python identifiers are reported."""
    yml = _write(
        tmp_path / "tools.yaml",
        """
        defaults:
          base_url: https://api.example.com
        tools:
          - name: get
            method: GET
            path: /items
            parameters:
              - name: "invalid-name"
                type: string
                location: query
                required: false
        """,
    )
    errors, _ = check_yaml(yml)
    assert any("not a valid Python identifier" in e for e in errors)


def test_placeholder_ok_flag_in_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """CLI --placeholder-ok flag produces 'OK (placeholder-ok)' output."""
    yml = _write(
        tmp_path / "tools.yaml",
        """
        tools:
          - name: t
            method: GET
            path: https://api.example.com/x
            static_headers:
              X-Api-Token: "{% .Secrets.MY_TOKEN %}"
        """,
    )
    rc = doctor_main([str(yml), "--placeholder-ok"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "placeholder-ok" in out
