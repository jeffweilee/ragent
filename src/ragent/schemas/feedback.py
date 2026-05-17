"""Pydantic request schema for `POST /feedback/v1` (T-FB.6, B52)."""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from pydantic import BaseModel, Field, field_validator

VOTE_LIKE: Final[int] = 1
VOTE_DISLIKE: Final[int] = -1
_ALLOWED_VOTES: Final[frozenset[int]] = frozenset({VOTE_LIKE, VOTE_DISLIKE})


class FeedbackReason(StrEnum):
    """B52 — frozen Day 1; new values require a new B-row."""

    IRRELEVANT = "irrelevant"
    HALLUCINATED = "hallucinated"
    OUTDATED = "outdated"
    INCOMPLETE = "incomplete"
    WRONG_CITATION = "wrong_citation"
    OTHER = "other"


class SourceRef(BaseModel):
    """Document identity per B11/B35/B39/B41 — both fields are required.

    Used inside ``FeedbackRequest.shown_sources`` and bound into the HMAC
    ``sources_hash`` so the client cannot rewrite the shown set under a
    re-used token.
    """

    source_app: str = Field(..., min_length=1, max_length=64)
    source_id: str = Field(..., min_length=1, max_length=128)


class FeedbackRequest(BaseModel):
    request_id: str = Field(..., min_length=1, max_length=64)
    feedback_token: str = Field(..., min_length=1)
    query_text: str = Field(..., min_length=1, max_length=8192)
    shown_sources: list[SourceRef] = Field(..., min_length=1, max_length=200)
    source_app: str = Field(..., min_length=1, max_length=64)
    source_id: str = Field(..., min_length=1, max_length=128)
    vote: int
    reason: FeedbackReason | None = None
    position_shown: int | None = Field(default=None, ge=0)

    @field_validator("vote")
    @classmethod
    def _validate_vote(cls, v: int) -> int:
        if v not in _ALLOWED_VOTES:
            raise ValueError(f"vote must be {VOTE_LIKE} or {VOTE_DISLIKE}")
        return v
