"""Event types for the twp-ai AG-UI style protocol."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


class RunStartedEvent(BaseModel):
    type: Literal["RUN_STARTED"] = "RUN_STARTED"
    run_id: str


class TextMessageStartEvent(BaseModel):
    type: Literal["TEXT_MESSAGE_START"] = "TEXT_MESSAGE_START"
    message_id: str


class TextMessageContentEvent(BaseModel):
    type: Literal["TEXT_MESSAGE_CONTENT"] = "TEXT_MESSAGE_CONTENT"
    message_id: str
    delta: str


class TextMessageEndEvent(BaseModel):
    type: Literal["TEXT_MESSAGE_END"] = "TEXT_MESSAGE_END"
    message_id: str


class CustomEvent(BaseModel):
    type: Literal["CUSTOM"] = "CUSTOM"
    name: str
    value: Any


class RunFinishedEvent(BaseModel):
    type: Literal["RUN_FINISHED"] = "RUN_FINISHED"
    run_id: str


class RunErrorEvent(BaseModel):
    type: Literal["RUN_ERROR"] = "RUN_ERROR"
    message: str
    code: str | None = None


def to_sse(event: BaseModel) -> str:
    return f"data: {event.model_dump_json()}\n\n"
