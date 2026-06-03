"""T-CA — ChatAgent proxy schemas."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

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


class ChatAgentV2Metadata(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session: str | None = None


class ChatAgentV2InputData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str


class ChatAgentV2Request(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metadata: ChatAgentV2Metadata = Field(default_factory=ChatAgentV2Metadata)
    inputData: ChatAgentV2InputData
    stream: bool = False
