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


RETRIEVE_TOOL = Tool(
    name="retrieve",
    description=(
        "Retrieve ranked document chunks from the ragent knowledge corpus. "
        "Use when you need to ground a response in the organisation's internal documents — "
        "runs hybrid semantic + keyword search and returns raw excerpts with source metadata "
        "(score, document_id, title, source_app). "
        "Does NOT synthesise an answer: read the returned [資料來源 #N] chunks and "
        "compose your response from them. Results are ordered by descending relevance."
    ),
    annotations=ToolAnnotations(readOnlyHint=True),
    inputSchema=_build_mcp_input_schema(_RetrieveArgs),
)
