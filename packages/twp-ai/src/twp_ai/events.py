"""SSE event types for the twp-ai AG-UI style protocol.

Adding a new event:
  1. Subclass BaseEvent with a unique Literal `type`.
  2. Add it to the Event union below.
  3. Emit it with to_sse() — nothing else changes.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class BaseEvent(BaseModel):
    """Foundation for every twp-ai SSE event.

    extra="allow" lets new fields pass through to older consumers without
    breaking deserialization — mirrors the ag-ui ConfiguredBaseModel pattern.
    """

    model_config = ConfigDict(extra="allow")


class RunStartedEvent(BaseEvent):
    type: Literal["RUN_STARTED"] = "RUN_STARTED"
    run_id: str


class TextMessageStartEvent(BaseEvent):
    type: Literal["TEXT_MESSAGE_START"] = "TEXT_MESSAGE_START"
    message_id: str


class TextMessageContentEvent(BaseEvent):
    type: Literal["TEXT_MESSAGE_CONTENT"] = "TEXT_MESSAGE_CONTENT"
    message_id: str
    delta: str


class TextMessageEndEvent(BaseEvent):
    type: Literal["TEXT_MESSAGE_END"] = "TEXT_MESSAGE_END"
    message_id: str


class CustomEvent(BaseEvent):
    """Application-defined event — name identifies the action, value is freeform."""

    type: Literal["CUSTOM"] = "CUSTOM"
    name: str
    value: Any


class RunFinishedEvent(BaseEvent):
    type: Literal["RUN_FINISHED"] = "RUN_FINISHED"
    run_id: str


class RunErrorEvent(BaseEvent):
    type: Literal["RUN_ERROR"] = "RUN_ERROR"
    message: str
    code: str | None = None


# Discriminated union consumed by the FE for type-safe deserialization.
# Extend this when adding new event types.
Event = Annotated[
    Union[
        RunStartedEvent,
        TextMessageStartEvent,
        TextMessageContentEvent,
        TextMessageEndEvent,
        CustomEvent,
        RunFinishedEvent,
        RunErrorEvent,
    ],
    Field(discriminator="type"),
]


def to_sse(event: BaseEvent) -> str:
    """Serialise any event to an SSE data line."""
    return f"data: {event.model_dump_json()}\n\n"
