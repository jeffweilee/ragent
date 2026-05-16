"""Dynamic MCP Hub: turn REST APIs declared in tools.yaml into MCP Tools.

The critical contract here is the *dynamic signature*: each tool function is
built with `inspect.Signature`/`inspect.Parameter` so that FastMCP's schema
inference produces a precise Pydantic/JSON Schema for the LLM, even though
the tool list is discovered at startup.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, get_args

import httpx
import yaml
from fastmcp import FastMCP

_TYPE_MAP: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}

Location = Literal["path", "query", "body", "header"]
_VALID_LOCATIONS: frozenset[str] = frozenset(get_args(Location))
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})
_MISSING: Any = inspect.Parameter.empty


@dataclass(frozen=True)
class _ParamSpec:
    name: str
    py_type: type
    location: Location
    required: bool
    default: Any
    description: str | None


@dataclass(frozen=True)
class _ToolSpec:
    name: str
    description: str
    method: str
    path: str
    params: tuple[_ParamSpec, ...]


def _parse_param(raw: dict[str, Any]) -> _ParamSpec:
    name = raw["name"]
    type_key = raw.get("type")
    if type_key not in _TYPE_MAP:
        raise ValueError(f"param {name!r}: unsupported type {type_key!r}")
    location = raw.get("location", "query")
    if location not in _VALID_LOCATIONS:
        raise ValueError(f"param {name!r}: invalid location {location!r}")
    required = bool(raw.get("required", False))
    default = _MISSING if required else raw.get("default")
    return _ParamSpec(
        name=name,
        py_type=_TYPE_MAP[type_key],
        location=location,
        required=required,
        default=default,
        description=raw.get("description"),
    )


def _parse_tool(raw: dict[str, Any]) -> _ToolSpec:
    method = str(raw["method"]).upper()
    return _ToolSpec(
        name=raw["name"],
        description=raw.get("description", ""),
        method=method,
        path=raw["path"],
        params=tuple(_parse_param(p) for p in raw.get("parameters") or []),
    )


def load_tools_yaml(path: str | Path) -> tuple[dict[str, Any], list[_ToolSpec]]:
    """Parse tools.yaml into (defaults, tool specs). Raises on schema errors."""
    with open(path, encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
    defaults = doc.get("defaults") or {}
    tools = [_parse_tool(t) for t in doc.get("tools") or []]
    seen: set[str] = set()
    for t in tools:
        if t.name in seen:
            raise ValueError(f"duplicate tool name: {t.name!r}")
        seen.add(t.name)
    return defaults, tools


def _build_signature(spec: _ToolSpec) -> inspect.Signature:
    """Produce a real Signature so FastMCP can derive a precise JSON Schema."""
    parameters: list[inspect.Parameter] = []
    for p in spec.params:
        annotation = p.py_type if p.required else (p.py_type | None)
        parameters.append(
            inspect.Parameter(
                name=p.name,
                kind=inspect.Parameter.KEYWORD_ONLY,
                default=p.default,
                annotation=annotation,
            )
        )
    return inspect.Signature(parameters=parameters, return_annotation=dict)


def _make_tool_callable(
    spec: _ToolSpec,
    client: httpx.AsyncClient,
    base_url: str,
) -> Any:
    locations = {p.name: p.location for p in spec.params}
    accepts_body = spec.method in _BODY_METHODS
    url_base = base_url.rstrip("/")

    async def _call(**kwargs: Any) -> dict[str, Any]:
        path_args: dict[str, Any] = {}
        query: dict[str, Any] = {}
        headers: dict[str, str] = {}
        body: dict[str, Any] = {}

        for name, value in kwargs.items():
            loc = locations.get(name)
            if loc is None:
                continue
            if loc == "path":
                path_args[name] = value
            elif loc == "query":
                if value is not None:
                    query[name] = value
            elif loc == "header":
                if value is not None:
                    headers[name.replace("_", "-")] = str(value)
            elif loc == "body" and value is not None:
                body[name] = value

        url = url_base + spec.path.format(**path_args)
        request_kwargs: dict[str, Any] = {}
        if query:
            request_kwargs["params"] = query
        if headers:
            request_kwargs["headers"] = headers
        if accepts_body and body:
            request_kwargs["json"] = body

        resp = await client.request(spec.method, url, **request_kwargs)
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "")
        payload: Any = resp.json() if "application/json" in ctype else resp.text
        return {"status": resp.status_code, "data": payload}

    sig = _build_signature(spec)
    _call.__signature__ = sig  # type: ignore[attr-defined]
    _call.__name__ = spec.name
    _call.__qualname__ = spec.name
    _call.__doc__ = spec.description or None
    _call.__annotations__ = {p.name: p.annotation for p in sig.parameters.values()}
    _call.__annotations__["return"] = dict
    return _call


def build_hub(
    yaml_path: str | Path,
    *,
    name: str = "ragent-mcp-hub",
    client: httpx.AsyncClient | None = None,
) -> tuple[FastMCP, httpx.AsyncClient]:
    """Construct a FastMCP server with every tool declared in tools.yaml.

    Returns (hub, client) so the caller owns the httpx client's lifecycle —
    the hub does not close clients it did not create either, so callers
    passing their own client must still close it themselves.
    """
    defaults, tools = load_tools_yaml(yaml_path)
    base_url = defaults.get("base_url", "")
    timeout = float(defaults.get("timeout", 30.0))
    default_headers = defaults.get("headers") or {}

    http = client or httpx.AsyncClient(timeout=timeout, headers=default_headers)
    mcp: FastMCP = FastMCP(name)
    for spec in tools:
        fn = _make_tool_callable(spec, http, base_url)
        mcp.add_tool(fn)
    return mcp, http
