"""T-CAUI.1 — AGENTIC_UI_TOOL envelope contract (name + unwrap)."""

from __future__ import annotations

import json

import pytest
from twp_ai.client_tools import AGENTIC_UI_TOOL_NAME, unwrap_agentic_ui_call


def test_name_constant() -> None:
    assert AGENTIC_UI_TOOL_NAME == "AGENTIC_UI_TOOL"


def test_unwrap_returns_inner_name_and_arguments_json() -> None:
    envelope = json.dumps({"tool_name": "fill_form", "arguments": {"description": "優化後的文案"}})

    name, args_json = unwrap_agentic_ui_call(envelope)

    assert name == "fill_form"
    # arguments come back as a JSON string (the TOOL_CALL_ARGS.delta wire shape),
    # non-ASCII preserved so the frontend gets the literal value.
    assert json.loads(args_json) == {"description": "優化後的文案"}
    assert "優化後的文案" in args_json


def test_unwrap_defaults_missing_arguments_to_empty_object() -> None:
    name, args_json = unwrap_agentic_ui_call(json.dumps({"tool_name": "ping"}))

    assert name == "ping"
    assert json.loads(args_json) == {}


def test_unwrap_accepts_pre_parsed_dict() -> None:
    # Some providers emit structured (already-parsed) tool-call arguments.
    name, args_json = unwrap_agentic_ui_call(
        {"tool_name": "fill_form", "arguments": {"description": "x"}}
    )

    assert name == "fill_form"
    assert json.loads(args_json) == {"description": "x"}


def test_unwrap_null_inner_arguments_degrades_to_empty_object() -> None:
    # An explicit "arguments": null must become {}, not the string "null"
    # (which the frontend would parse to a null value).
    name, args_json = unwrap_agentic_ui_call(json.dumps({"tool_name": "ping", "arguments": None}))

    assert name == "ping"
    assert args_json == "{}"


@pytest.mark.parametrize("bad_inner", ['"a string"', "[1, 2]", "42"])
def test_unwrap_non_object_inner_arguments_raises(bad_inner: str) -> None:
    with pytest.raises(ValueError):
        unwrap_agentic_ui_call(f'{{"tool_name": "ping", "arguments": {bad_inner}}}')


def test_unwrap_non_str_non_dict_input_raises() -> None:
    with pytest.raises(ValueError):
        unwrap_agentic_ui_call(42)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "bad",
    [
        "not json at all",
        json.dumps(["not", "an", "object"]),
        json.dumps({"arguments": {"x": 1}}),  # missing tool_name
        json.dumps({"tool_name": "", "arguments": {}}),  # empty tool_name
        json.dumps({"tool_name": 42, "arguments": {}}),  # non-string tool_name
    ],
)
def test_unwrap_malformed_envelope_raises_value_error(bad: str) -> None:
    with pytest.raises(ValueError):
        unwrap_agentic_ui_call(bad)
