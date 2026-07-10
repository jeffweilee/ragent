"""Secret rendering: {% .Secrets.KEY %} → env var value at startup.

Contracts:
- render_secrets replaces all placeholders from env; fails fast on missing key.
- Secret values NEVER appear in KeyError messages (key name only).
- PLACEHOLDER_ENV returns "PLACEHOLDER" for any key (doctor --placeholder-ok mode).
- validate_placeholder_syntax catches bad key format and {{ }} Helm conflict.
"""

from __future__ import annotations

import pytest

from mcp_hub._render import (
    PLACEHOLDER_ENV,
    render_secrets,
    validate_placeholder_syntax,
)


def test_render_substitutes_all_placeholders():
    content = (
        "X-Api-Token: \"{% .Secrets.API_TOKEN %}\"\n"
        "X-Other: \"{% .Secrets.OTHER_KEY %}\"\n"
    )
    env = {"API_TOKEN": "tok-abc", "OTHER_KEY": "val-xyz"}
    result = render_secrets(content, env)
    assert '"tok-abc"' in result
    assert '"val-xyz"' in result
    assert "{% " not in result


def test_render_raises_on_missing_key():
    content = 'X-Api-Token: "{% .Secrets.MISSING_KEY %}"'
    with pytest.raises(KeyError):
        render_secrets(content, {})


def test_render_missing_key_error_contains_key_name_not_value():
    """KeyError message must contain the key name so operator knows what to set,
    but must NOT contain any secret value (there is none here — just a guard)."""
    content = 'X-Token: "{% .Secrets.MY_SECRET_KEY %}"'
    with pytest.raises(KeyError) as exc_info:
        render_secrets(content, {})
    assert "MY_SECRET_KEY" in str(exc_info.value)


def test_render_no_placeholders_returns_unchanged():
    content = "X-Static: literal-value\n"
    result = render_secrets(content, {})
    assert result == content


def test_placeholder_env_returns_placeholder_for_any_key():
    assert PLACEHOLDER_ENV["ANY_KEY"] == "PLACEHOLDER"
    assert PLACEHOLDER_ENV["ANOTHER_KEY"] == "PLACEHOLDER"
    assert "WHATEVER" in PLACEHOLDER_ENV


def test_placeholder_env_with_render_replaces_all():
    content = (
        'token: "{% .Secrets.TOKEN %}"\n'
        'secret: "{% .Secrets.SECRET %}"\n'
    )
    result = render_secrets(content, PLACEHOLDER_ENV)
    assert "PLACEHOLDER" in result
    assert "{% " not in result


def test_validate_placeholder_syntax_accepts_valid_keys():
    content = 'X-Token: "{% .Secrets.API_TOKEN %}"\nX-Other: "{% .Secrets.DB_PASSWORD %}"'
    errors = validate_placeholder_syntax(content)
    assert errors == []


def test_validate_placeholder_syntax_rejects_lowercase_key():
    content = 'X-Token: "{% .Secrets.lowercase_key %}"'
    errors = validate_placeholder_syntax(content)
    assert len(errors) >= 1
    assert any("lowercase_key" in e or "placeholder" in e.lower() for e in errors)


def test_validate_placeholder_syntax_rejects_helm_double_brace():
    """{{ }} would be interpreted by Helm before the YAML reaches the pod."""
    content = 'X-Token: "{{ .Values.token }}"'
    errors = validate_placeholder_syntax(content)
    assert any("{{" in e for e in errors)


def test_validate_placeholder_syntax_rejects_mixed():
    content = (
        'X-Good: "{% .Secrets.GOOD_KEY %}"\n'
        'X-Bad: "{{ .Values.bad }}"\n'
        'X-Lower: "{% .Secrets.bad_key %}"\n'
    )
    errors = validate_placeholder_syntax(content)
    assert len(errors) >= 2
