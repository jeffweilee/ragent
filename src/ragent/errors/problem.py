"""T2.14 — RFC 9457 Problem Details builder (B5)."""

from __future__ import annotations

from fastapi.responses import JSONResponse

_BASE = "https://ragent.dev/errors"


def problem(
    status: int,
    error_code: str,
    title: str,
    detail: str = "",
    instance: str = "",
    errors: list[dict] | None = None,
    extra: dict | None = None,
) -> JSONResponse:
    body: dict = {
        "type": f"{_BASE}/{error_code.lower().replace('_', '-')}",
        "title": title,
        "status": status,
        "error_code": error_code,
    }
    if detail:
        body["detail"] = detail
    if instance:
        body["instance"] = instance
    if errors is not None:
        body["errors"] = errors
    if extra:
        body.update(extra)
    return JSONResponse(
        content=body,
        status_code=status,
        media_type="application/problem+json",
    )
