"""Secret placeholder rendering: {% .Secrets.KEY %} → env[KEY].

Called once per YAML file at startup, before yaml.safe_load(). Fails fast on
any missing key so the process never starts with a literal placeholder that
would reach an upstream service.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

# Matches {% .Secrets.KEY %} where KEY is uppercase ASCII + digits + underscores.
_SECRET_RE = re.compile(r"\{%\s*\.Secrets\.([A-Z][A-Z0-9_]*)\s*%\}")
# Broader pattern for validation — catches any {% .Secrets.* %} including bad keys.
_ANY_SECRET_RE = re.compile(r"\{%\s*\.Secrets\.(\S+?)\s*%\}")
_VALID_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def render_secrets(content: str, env: Mapping[str, str]) -> str:
    """Replace all {% .Secrets.KEY %} with env[KEY].

    Raises KeyError with a human-readable message if any key is absent.
    Intentional fail-fast: a CrashLoop is visible; a literal placeholder
    silently reaching upstream is not.
    """

    def _sub(m: re.Match[str]) -> str:
        key = m.group(1)
        if key not in env:
            raise KeyError(
                f"Secret placeholder '{{% .Secrets.{key} %}}' not found in environment. "
                f"Ensure {key!r} is listed in the VaultSecret CR and synced before deploying."
            )
        return env[key]

    return _SECRET_RE.sub(_sub, content)


def validate_placeholder_syntax(content: str) -> list[str]:
    """Return format errors without resolving against env (doctor --placeholder-ok mode).

    Checks:
    - KEY names match [A-Z][A-Z0-9_]* (valid identifiers, not arbitrary strings).
    - No {{ present (Helm would template it before mcp-hub sees the file).
    """
    errors: list[str] = []
    for m in _ANY_SECRET_RE.finditer(content):
        key = m.group(1)
        if not _VALID_KEY_RE.match(key):
            errors.append(
                f"invalid secret placeholder key {key!r}: must match [A-Z][A-Z0-9_]* "
                f"(uppercase, digits, underscores, start with letter)"
            )
    if "{{" in content:
        errors.append(
            "content contains '{{' which Helm templates before mcp-hub renders it; "
            "use {% .Secrets.KEY %} syntax only"
        )
    return errors


class _PlaceholderEnv(Mapping):
    """Pseudo-env that returns 'PLACEHOLDER' for every key.

    Used by doctor --placeholder-ok so yaml.safe_load can run on
    placeholder-containing YAML without needing real secrets.
    """

    def __getitem__(self, key: str) -> str:
        return "PLACEHOLDER"

    def __iter__(self):
        return iter([])

    def __len__(self) -> int:
        return 0

    def __contains__(self, key: object) -> bool:
        return True


PLACEHOLDER_ENV: Mapping[str, str] = _PlaceholderEnv()
