"""T8.D3 — Anti-drift lint: routers must not redeclare auth headers.

The Swagger Authorize button (T8.D1) and the resolved-value injection
(T8.D2 ``get_user_id`` dep) together replace every per-route
``Header(alias="X-User-Id")`` / ``Header(alias="X-Auth-Token")``
declaration. This test fails CI if anyone re-introduces one — the auth
header must come from the middleware-driven dep, NOT the route signature,
or Swagger and the gate drift apart again.
"""

from __future__ import annotations

import pathlib
import re

_ROUTERS_DIR = pathlib.Path(__file__).resolve().parents[2] / "src" / "ragent" / "routers"
_FORBIDDEN = re.compile(r"""Header\s*\(\s*alias\s*=\s*['"](X-User-Id|X-Auth-Token)['"]""")


def test_no_auth_header_redeclared_in_any_router() -> None:
    offenders: list[str] = []
    for py in _ROUTERS_DIR.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        if _FORBIDDEN.search(text):
            offenders.append(str(py.relative_to(_ROUTERS_DIR.parents[2])))
    assert not offenders, (
        "Auth headers must come from Depends(get_user_id), not per-route "
        f"Header(alias=...). Offending files: {offenders}"
    )
