"""T8.D2 — Single source of truth for the resolved ``user_id`` in route handlers.

``get_user_id`` reads ``request.scope[SCOPE_USER_ID_KEY]`` populated by
``_x_user_id_middleware`` (both trust-header and JWT modes). Routes inject it
via ``Depends(get_user_id)`` and so no longer redeclare ``Header(alias=...)``,
which means:

* Adding a new router cannot "forget" to declare the auth header — the
  active scheme is published once by ``install_openapi`` (T8.D1) and the
  resolved value reaches handlers through this dep regardless.
* Renaming ``RAGENT_USER_ID_HEADER`` / ``RAGENT_JWT_HEADER`` no longer
  drifts away from per-route literals.

The header fallback exists for unit tests that construct a single router
under a bare ``FastAPI()`` (no middleware) — production traffic ALWAYS goes
through ``_x_user_id_middleware`` which populates the scope key first.
"""

from __future__ import annotations

from fastapi import Request

from ragent.middleware.logging import _USER_ID_HEADER, SCOPE_USER_ID_KEY


async def get_user_id(request: Request) -> str | None:
    scoped = request.scope.get(SCOPE_USER_ID_KEY)
    if scoped:
        return scoped
    return request.headers.get(_USER_ID_HEADER) or None
