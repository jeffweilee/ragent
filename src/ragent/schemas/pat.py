"""PAT router I/O schemas (T-PAT)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class PatAuthorizeRequest(BaseModel):
    """`POST /pat/v1/authorize` body. The SSO identity comes from the request
    header (never the body); only the PAT itself is carried here — temporary
    until the fetch-PAT API replaces the manual submission."""

    model_config = ConfigDict(populate_by_name=True)

    pat_token: str = Field(..., alias="patToken", min_length=1)
