"""Pydantic I/O schemas for the `/pat/v1` router (T-PAT).

The owner (`user_id`) is NEVER part of these schemas — it is resolved from the
request and supplied by the router, so a client cannot read another user's
authorization state. Neither is the PAT itself, nor any fragment of it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

# `none`  — never authorized, or revoked. Show a first-time authorize CTA.
# `active` — authorized and usable.
# `invalid` — authorization exists but cannot be used; re-authorize.
#
# Three states, not two: the frontend copy differs ("authorize to enable X" vs
# "your authorization expired"), and operationally a spike in `invalid` means the
# upstream PAT service is failing while a spike in `none` just means new users.
PatStatus = Literal["none", "active", "invalid"]


class PatStatusResponse(BaseModel):
    status: PatStatus
    # When the user last authorized by hand. NOT `updated_at`, which every 12 h
    # refresh overwrites, and NOT `created_at`, which only records the first ever
    # authorization — re-authorizing restarts the window.
    authorized_at: str | None = None  # ISO 8601 UTC
    # End of the authorization window (`YYYY-MM-DD`). Unlike the 12 h `exp`,
    # this deadline is real: nothing renews it without the user. `null` for rows
    # written before migration 018 — unknown, not "no expiry".
    authorization_expires_at: str | None = None
