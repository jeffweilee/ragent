"""Single source of truth for RAGENT_AUTH_MODE resolution."""

from __future__ import annotations

from ragent.utility.compat import StrEnum
from ragent.utility.env import str_env


class AuthMode(StrEnum):
    none = "none"
    user_header = "user_header"
    jwt_header = "jwt_header"
    jwt_prefer_header = "jwt_prefer_header"


def parse_auth_mode() -> AuthMode:
    raw = str_env("RAGENT_AUTH_MODE", AuthMode.user_header.value).strip()
    try:
        return AuthMode(raw)
    except ValueError:
        valid = ", ".join(m.value for m in AuthMode)
        raise ValueError(
            f"RAGENT_AUTH_MODE='{raw}' is not valid; must be one of: {valid}"
        ) from None
