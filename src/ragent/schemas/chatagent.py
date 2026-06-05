"""T-CA — ChatAgent proxy schemas."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from ragent.schemas.chat import ChatRequest


class ChatAgentRequest(ChatRequest):
    """Extends ChatRequest with an optional caller-supplied session ID.

    When absent the router mints a new_id() per request.
    """

    session: str | None = None


class SessionRenameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session: str
    sessionName: str


class SessionDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session: str
