"""MCP tool descriptor for `retrieve` (§3.8.3)."""

from __future__ import annotations

from typing import Any

from mcp.types import Tool, ToolAnnotations
from pydantic import ConfigDict

from ragent.schemas.retrieve import RetrieveRequest


class _RetrieveArgs(RetrieveRequest):
    """extra=forbid: MCP callers must not send undeclared fields."""

    model_config = ConfigDict(extra="forbid")


def _build_mcp_input_schema(model: type) -> dict[str, Any]:
    """Pydantic v2 emits anyOf:[{type:T,...},{type:null}] for Optional fields;
    MCP clients expect plain Draft-7 {type:T,...}. Collapses the union and
    strips auto-generated title fields."""
    schema = model.model_json_schema()
    schema.pop("title", None)
    props = schema.get("properties", {})
    for name in list(props):
        prop = dict(props[name])
        prop.pop("title", None)
        if "anyOf" in prop:
            non_null = [s for s in prop["anyOf"] if s.get("type") != "null"]
            if len(non_null) == 1:
                merged = {k: v for k, v in prop.items() if k != "anyOf"}
                merged.update(non_null[0])
                # After stripping the null branch, "default": null is
                # contradictory — null is no longer a valid value. Optionality
                # is signalled by absence from "required"; drop the null default
                # so clients don't materialise and submit it.
                if "default" in merged and merged["default"] is None:
                    del merged["default"]
                prop = merged
        props[name] = prop
    schema["properties"] = props
    schema.setdefault("additionalProperties", False)
    return schema


# Matches doc_to_source_entry() output verbatim — every key is always present,
# nullable when the source document lacks the field.
_NULLABLE_STRING: dict[str, Any] = {"type": ["string", "null"]}

RETRIEVE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["sources"],
    "properties": {
        "sources": {
            "type": "array",
            "description": (
                "Retrieved sources ordered by descending relevance. "
                "Pass this list to the UI's retrieved-sources panel."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "document_id",
                    "source_app",
                    "source_id",
                    "source_meta",
                    "type",
                    "source_title",
                    "source_url",
                    "mime_type",
                    "excerpt",
                    "score",
                ],
                "properties": {
                    "document_id": _NULLABLE_STRING,
                    "source_app": _NULLABLE_STRING,
                    "source_id": _NULLABLE_STRING,
                    "source_meta": _NULLABLE_STRING,
                    "type": {"type": "string"},
                    "source_title": _NULLABLE_STRING,
                    "source_url": _NULLABLE_STRING,
                    "mime_type": _NULLABLE_STRING,
                    "excerpt": {"type": "string"},
                    "score": {"type": ["number", "null"]},
                },
            },
        },
    },
}

RETRIEVE_TOOL = Tool(
    name="retrieve",
    description=(
        "Retrieve ranked document chunks from the ragent knowledge corpus. "
        "Use when you need to ground a response in the organisation's internal documents — "
        "runs hybrid semantic + keyword search. Results are ordered by descending relevance. "
        "structuredContent.sources is the machine-readable source list: pass it to the UI's "
        "retrieved-sources panel. The text content is a <context>-delimited block with a "
        "citation table and [N] excerpt sections: ground your answer on the excerpts and "
        "cite by [N] — do NOT transcribe the <context> block verbatim into your reply. "
        "Does NOT synthesise an answer."
    ),
    annotations=ToolAnnotations(readOnlyHint=True),
    inputSchema=_build_mcp_input_schema(_RetrieveArgs),
    outputSchema=RETRIEVE_OUTPUT_SCHEMA,
)
