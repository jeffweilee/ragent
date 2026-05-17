"""Dynamic MCP Hub: turn REST APIs declared in tools.yaml into MCP Tools.

The critical contract here is the *dynamic signature*: each tool function is
built with `inspect.Signature`/`inspect.Parameter` so that FastMCP's schema
inference produces a precise Pydantic/JSON Schema for the LLM, even though
the tool list is discovered at startup.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import re
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, get_args

import httpx
import structlog
import yaml
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

logger = structlog.get_logger(__name__)

_INCOMING_HEADERS: ContextVar[dict[str, str] | None] = ContextVar(
    "mcp_hub_incoming_headers", default=None
)
# Templates in forward_headers values reference incoming headers by lowercase
# name, matching the ASGI-canonical form populated by HeaderForwardMiddleware.
_TEMPLATE_PLACEHOLDER = re.compile(r"\{([a-z0-9][a-z0-9._-]*)\}")

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

_UPSTREAM_BODY_MAX_BYTES = 4096
_REQUEST_ID_HEADERS = ("x-request-id", "x-correlation-id", "request-id")

_ERR_UPSTREAM_4XX = "upstream_4xx"
_ERR_UPSTREAM_5XX = "upstream_5xx"
_ERR_TIMEOUT = "timeout"
_ERR_CONNECT = "connect_error"


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
    name: str  # fully-qualified: f"{system}.{raw_name}"
    description: str
    method: str
    path: str
    params: tuple[_ParamSpec, ...]
    system: str = ""
    base_url: str | None = None
    timeout: float | None = None
    static_headers: dict[str, str] = field(default_factory=dict)
    # outgoing-header-name -> template string where `{x-foo}` substitutes the
    # incoming header `x-foo` (lowercased). Missing placeholders skip the
    # entire outgoing header (graceful degradation).
    forward_headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _SystemSpec:
    """One yaml file = one system. Drives the per-system httpx.AsyncClient."""

    name: str
    base_url: str
    timeout: float
    max_connections: int
    default_headers: dict[str, str]
    source: Path

    def make_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self.timeout,
            limits=httpx.Limits(max_connections=self.max_connections),
            headers=self.default_headers or None,
        )


@dataclass
class LoadFailure:
    """A single file or tool that could not be loaded. Reported through
    LoadResult.failures (and via HubBundle.failures at runtime)."""

    source: str  # e.g. "tools.d/billing.yaml" or "tools.d/billing.yaml:create_invoice"
    reason: str


@dataclass
class LoadResult:
    """Outcome of loading one or more yaml files."""

    tools: list[_ToolSpec]
    systems: dict[str, _SystemSpec]
    failures: list[LoadFailure]


@dataclass
class HubBundle:
    """Returned by build_hub: the FastMCP server, per-system httpx clients
    (caller owns the lifecycle — close each in shutdown), and any load
    failures the operator should know about."""

    hub: FastMCP
    clients: dict[str, httpx.AsyncClient]
    failures: list[LoadFailure]


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


def _parse_headers(raw: Any, owner: str) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"{owner}: headers must be a mapping, got {type(raw).__name__}")
    return {str(k): str(v) for k, v in raw.items()}


def _parse_tool(raw: dict[str, Any]) -> _ToolSpec:
    method = str(raw["method"]).upper()
    name = raw["name"]
    static_headers = _parse_headers(
        raw.get("static_headers"), owner=f"tool {name!r} static_headers"
    )
    forward_headers = _parse_headers(
        raw.get("forward_headers"), owner=f"tool {name!r} forward_headers"
    )

    overlap = {h.lower() for h in static_headers}.intersection({h.lower() for h in forward_headers})
    if overlap:
        raise ValueError(
            f"tool {name!r}: header(s) {sorted(overlap)} declared in both "
            f"static_headers and forward_headers"
        )

    params = tuple(_parse_param(p) for p in raw.get("parameters") or [])
    header_arg_names = {p.name.replace("_", "-").lower() for p in params if p.location == "header"}
    config_header_names = {h.lower() for h in static_headers} | {h.lower() for h in forward_headers}
    collisions = header_arg_names & config_header_names
    if collisions:
        raise ValueError(
            f"tool {name!r}: header parameter(s) {sorted(collisions)} collide with "
            f"static_headers/forward_headers (would silently fight at request time)"
        )

    timeout_raw = raw.get("timeout")
    timeout = float(timeout_raw) if timeout_raw is not None else None

    return _ToolSpec(
        name=name,
        description=raw.get("description", ""),
        method=method,
        path=raw["path"],
        params=params,
        base_url=raw.get("base_url"),
        timeout=timeout,
        static_headers=static_headers,
        forward_headers=forward_headers,
    )


_YAML_SUFFIXES = (".yaml", ".yml")
_DEFAULT_TIMEOUT = 30.0
_DEFAULT_MAX_CONNECTIONS = 100


def _parse_system_spec(doc: dict[str, Any], source: Path) -> _SystemSpec:
    defaults = doc.get("defaults") or {}
    return _SystemSpec(
        name=str(doc.get("system") or source.stem),
        base_url=str(defaults.get("base_url") or ""),
        timeout=float(defaults.get("timeout", _DEFAULT_TIMEOUT)),
        max_connections=int(defaults.get("max_connections", _DEFAULT_MAX_CONNECTIONS)),
        default_headers=_parse_headers(defaults.get("headers"), owner=f"{source} defaults.headers"),
        source=source,
    )


def _record_failure(
    result: LoadResult,
    source: str,
    exc_or_msg: BaseException | str,
    *,
    strict: bool,
) -> None:
    """Either re-raise (strict mode) or append to result.failures."""
    if strict:
        if isinstance(exc_or_msg, BaseException):
            raise exc_or_msg
        raise ValueError(exc_or_msg)
    result.failures.append(LoadFailure(source=source, reason=str(exc_or_msg)))


def _load_one_file(source: Path, result: LoadResult, *, strict: bool) -> None:
    """Parse one yaml file; update result in place. In strict mode any failure
    raises; in non-strict mode failures are collected and the function returns
    after recording them."""
    try:
        with open(source, encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}
        if not isinstance(doc, dict):
            raise ValueError(f"top-level yaml must be a mapping, got {type(doc).__name__}")
        system = _parse_system_spec(doc, source)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        _record_failure(result, str(source), exc, strict=strict)
        return

    if system.name in result.systems:
        _record_failure(
            result,
            str(source),
            f"duplicate system name {system.name!r}: also defined in "
            f"{result.systems[system.name].source}",
            strict=strict,
        )
        return

    seen_in_system: set[str] = set()
    for raw_tool in doc.get("tools") or []:
        raw_name = raw_tool.get("name") if isinstance(raw_tool, dict) else None
        try:
            tool = _parse_tool(raw_tool)
        except (TypeError, ValueError, KeyError) as exc:
            _record_failure(result, f"{source}:{raw_name or '?'}", exc, strict=strict)
            continue
        if tool.name in seen_in_system:
            _record_failure(
                result,
                f"{source}:{tool.name}",
                f"duplicate tool name {tool.name!r} within system {system.name!r}",
                strict=strict,
            )
            continue
        seen_in_system.add(tool.name)
        result.tools.append(
            dataclasses.replace(
                tool,
                name=f"{system.name}.{tool.name}",
                system=system.name,
                base_url=tool.base_url or system.base_url or None,
                timeout=tool.timeout if tool.timeout is not None else system.timeout,
                static_headers={**system.default_headers, **tool.static_headers},
            )
        )

    result.systems[system.name] = system


def load_tools_yaml(path: str | Path, *, strict: bool = True) -> LoadResult:
    """Load a single yaml file OR a directory of yaml files.

    In directory mode, every `*.yaml` and `*.yml` file (sorted alphabetically)
    is treated as one independent SYSTEM. Tool names are auto-qualified as
    `<system>.<tool>` so different systems can use the same raw name.

    Single-file mode is a degenerate case of directory mode: the file itself
    is treated as one system named after its filename stem (or the explicit
    `system:` key inside the file).

    strict=True (default) raises on the first failure — use this in CI (doctor).
    strict=False collects failures into LoadResult.failures and keeps going so
    the Hub can still expose healthy tools when one file is broken.
    """
    p = Path(path)
    result = LoadResult(tools=[], systems={}, failures=[])

    if p.is_dir():
        files = sorted(fp for fp in p.iterdir() if fp.suffix in _YAML_SUFFIXES)
    elif p.exists():
        files = [p]
    else:
        msg = f"path does not exist: {p}"
        if strict:
            raise FileNotFoundError(msg)
        result.failures.append(LoadFailure(source=str(p), reason=msg))
        return result

    for fp in files:
        _load_one_file(fp, result, strict=strict)
    return result


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


def _extract_request_id(headers: httpx.Headers) -> str | None:
    for h in _REQUEST_ID_HEADERS:
        value = headers.get(h)
        if value:
            return value
    return None


def _base_upstream_error(
    resp: httpx.Response, error_type: str, request_id: str | None = None
) -> dict[str, Any]:
    err: dict[str, Any] = {"type": error_type, "status": resp.status_code}
    req_id = request_id if request_id is not None else _extract_request_id(resp.headers)
    if req_id:
        err["upstream_request_id"] = req_id
    return err


def _attach_body(err: dict[str, Any], body: str, raw_len: int) -> None:
    if raw_len > _UPSTREAM_BODY_MAX_BYTES:
        err["upstream_body"] = body[:_UPSTREAM_BODY_MAX_BYTES]
        err["truncated"] = True
    else:
        err["upstream_body"] = body


def _build_4xx_error(resp: httpx.Response, request_id: str | None = None) -> dict[str, Any]:
    err = _base_upstream_error(resp, _ERR_UPSTREAM_4XX, request_id)
    ctype = resp.headers.get("content-type", "")

    if "application/json" in ctype or "application/problem+json" in ctype:
        try:
            body = resp.json()
        except ValueError:
            body = None
        if body is not None:
            # Skip serialization on the common small-body path; the parsed
            # body's len-bound is resp.content size (JSON never expands on
            # re-serialization without whitespace).
            if len(resp.content) <= _UPSTREAM_BODY_MAX_BYTES:
                err["upstream_body"] = body
            else:
                serialized = json.dumps(body)
                _attach_body(err, serialized, len(serialized))
            return err

    if ctype.startswith("text/plain"):
        text = resp.text
        _attach_body(err, text, len(text))
        return err

    err["upstream_body_omitted"] = True
    err["upstream_content_type"] = ctype
    return err


def _render_forward_template(template: str, incoming: dict[str, str]) -> str | None:
    """Substitute `{header-name}` placeholders in `template` from `incoming`
    (keys lowercased). Return None if any referenced header is absent — the
    caller will then skip the outgoing header entirely."""
    missing = False

    def _sub(match: re.Match[str]) -> str:
        nonlocal missing
        value = incoming.get(match.group(1))
        if value is None:
            missing = True
            return ""
        return value

    rendered = _TEMPLATE_PLACEHOLDER.sub(_sub, template)
    return None if missing else rendered


def _make_tool_callable(
    spec: _ToolSpec,
    client: httpx.AsyncClient,
    base_url: str = "",
) -> Any:
    locations = {p.name: p.location for p in spec.params}
    accepts_body = spec.method in _BODY_METHODS
    effective_base = (spec.base_url or base_url).rstrip("/")

    async def _call(**kwargs: Any) -> dict[str, Any]:
        path_args: dict[str, Any] = {}
        query: dict[str, Any] = {}
        headers: dict[str, str] = dict(spec.static_headers)
        incoming = _INCOMING_HEADERS.get() or {}
        for outgoing, template in spec.forward_headers.items():
            rendered = _render_forward_template(template, incoming)
            if rendered is not None:
                headers[outgoing] = rendered
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

        rendered = spec.path.format(**path_args)
        if rendered.startswith(("http://", "https://")):
            url = rendered
        else:
            url = effective_base + rendered
        request_kwargs: dict[str, Any] = {}
        if query:
            request_kwargs["params"] = query
        if headers:
            request_kwargs["headers"] = headers
        if accepts_body and body:
            request_kwargs["json"] = body
        if spec.timeout is not None:
            request_kwargs["timeout"] = spec.timeout

        request_id = incoming.get("x-request-id")
        log_ctx = {"tool": spec.name, "system": spec.system, "request_id": request_id}
        start = time.perf_counter()

        try:
            resp = await client.request(spec.method, url, **request_kwargs)
        except httpx.TimeoutException as exc:
            logger.error(
                "mcp_hub.timeout",
                latency_ms=int((time.perf_counter() - start) * 1000),
                configured_timeout=spec.timeout,
                **log_ctx,
            )
            raise ToolError(json.dumps({"type": _ERR_TIMEOUT, "message": str(exc)})) from exc
        except httpx.ConnectError as exc:
            logger.error(
                "mcp_hub.connect_error",
                latency_ms=int((time.perf_counter() - start) * 1000),
                error_type=type(exc).__name__,
                **log_ctx,
            )
            raise ToolError(json.dumps({"type": _ERR_CONNECT, "message": str(exc)})) from exc

        latency_ms = int((time.perf_counter() - start) * 1000)
        upstream_request_id = _extract_request_id(resp.headers)

        if resp.status_code >= 500:
            logger.error(
                "mcp_hub.upstream_5xx",
                status=resp.status_code,
                latency_ms=latency_ms,
                upstream_request_id=upstream_request_id,
                **log_ctx,
            )
            raise ToolError(
                json.dumps(_base_upstream_error(resp, _ERR_UPSTREAM_5XX, upstream_request_id))
            )

        if resp.status_code >= 400:
            logger.warning(
                "mcp_hub.upstream_4xx",
                status=resp.status_code,
                latency_ms=latency_ms,
                upstream_request_id=upstream_request_id,
                **log_ctx,
            )
            return {
                "ok": False,
                "status": resp.status_code,
                "error": _build_4xx_error(resp, upstream_request_id),
            }

        ctype = resp.headers.get("content-type", "")
        if "application/json" in ctype:
            try:
                payload: Any = resp.json()
            except ValueError:
                payload = resp.text
        else:
            payload = resp.text
        logger.info(
            "mcp_hub.tool_call.success",
            status=resp.status_code,
            latency_ms=latency_ms,
            **log_ctx,
        )
        return {"ok": True, "status": resp.status_code, "data": payload}

    sig = _build_signature(spec)
    _call.__signature__ = sig  # type: ignore[attr-defined]
    _call.__name__ = spec.name
    _call.__qualname__ = spec.name
    _call.__doc__ = spec.description or None
    _call.__annotations__ = {p.name: p.annotation for p in sig.parameters.values()}
    _call.__annotations__["return"] = dict
    return _call


def build_hub(yaml_path: str | Path, *, name: str = "ragent-mcp-hub") -> HubBundle:
    """Construct a FastMCP server from a yaml file or directory of yaml files.

    Each yaml file in the directory is one SYSTEM with its own httpx
    AsyncClient (independent timeout and connection pool). Tool names are
    auto-qualified as `<system>.<tool>` so different systems can declare the
    same raw name without conflict.

    Failures (bad yaml syntax, schema violation, `add_tool` rejection) are
    recorded on the returned bundle's `failures` list — healthy tools still
    serve. The caller owns the lifecycle of `bundle.clients`: close each in
    shutdown (server.py does this via ASGI lifespan).
    """
    result = load_tools_yaml(yaml_path, strict=False)
    clients = {name: spec.make_client() for name, spec in result.systems.items()}

    for failure in result.failures:
        logger.warning("mcp_hub.load_failure", source=failure.source, reason=failure.reason)

    mcp: FastMCP = FastMCP(name)
    registered = 0
    for spec in result.tools:
        client = clients.get(spec.system)
        if client is None:
            result.failures.append(
                LoadFailure(
                    source=spec.name,
                    reason=f"no client for system {spec.system!r}",
                )
            )
            continue
        try:
            fn = _make_tool_callable(spec, client)
            mcp.add_tool(fn)
            registered += 1
        except Exception as exc:  # noqa: BLE001 — must isolate any registration failure
            result.failures.append(LoadFailure(source=spec.name, reason=f"add_tool: {exc}"))
            logger.warning("mcp_hub.load_failure", source=spec.name, reason=f"add_tool: {exc}")

    for sys_name, sys_spec in result.systems.items():
        logger.info(
            "mcp_hub.system_configured",
            system=sys_name,
            base_url=sys_spec.base_url,
            timeout=sys_spec.timeout,
            max_connections=sys_spec.max_connections,
        )
    logger.info(
        "mcp_hub.ready",
        systems=sorted(result.systems),
        tool_count=registered,
        failure_count=len(result.failures),
    )

    return HubBundle(hub=mcp, clients=clients, failures=result.failures)
