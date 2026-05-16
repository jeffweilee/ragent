"""Pydantic request schema for `POST /feedback/v1` (T-FB.6, B52)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class FeedbackReason(StrEnum):
    """B52 — frozen Day 1; new values require a new B-row."""

    IRRELEVANT = "irrelevant"
    HALLUCINATED = "hallucinated"
    OUTDATED = "outdated"
    INCOMPLETE = "incomplete"
    WRONG_CITATION = "wrong_citation"
    OTHER = "other"


class FeedbackRequest(BaseModel):
    request_id: str = Field(..., min_length=1, max_length=64)
    feedback_token: str = Field(..., min_length=1)
    query_text: str = Field(..., min_length=1, max_length=8192)
    shown_source_ids: list[str] = Field(..., min_length=1, max_length=200)
    source_id: str = Field(..., min_length=1, max_length=128)
    vote: int
    reason: FeedbackReason | None = None
    position_shown: int | None = Field(default=None, ge=0)

    @field_validator("vote")
    @classmethod
    def _validate_vote(cls, v: int) -> int:
        if v not in (-1, 1):
            raise ValueError("vote must be +1 or -1")
        return v
