"""Dynamic MCP Hub: turn REST APIs declared in YAML into MCP Tools.

Each YAML file in the configured directory defines one upstream system.
Tool names are auto-qualified as <system>.<tool> so different systems can
use the same raw name without conflict.
"""

from __future__ import annotations

import base64
import dataclasses
import inspect
import json
import re
import time
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, get_args

import httpx
import structlog
import yaml
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from ._render import render_secrets
from .metrics import (
    record_mcp_hub_load_failure,
    record_mcp_hub_system_up,
    record_mcp_hub_tool_call,
    record_mcp_hub_tool_registered,
)

logger = structlog.get_logger(__name__)

_INCOMING_HEADERS: ContextVar[dict[str, str] | None] = ContextVar(
    "mcp_hub_incoming_headers", default=None
)
_TEMPLATE_PLACEHOLDER = re.compile(r"\{([a-z0-9][a-z0-9._-]*)\}")

_TYPE_MAP: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
    # base64-encoded content; hub decodes and sends as a multipart file field.
    # MCP clients see this as a string parameter.
    "file": str,
}

Location = Literal["path", "query", "body", "header"]
_VALID_LOCATIONS: frozenset[str] = frozenset(get_args(Location))
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})
_BODY_FORMATS = frozenset({"json", "form", "multipart"})
_MISSING: Any = inspect.Parameter.empty

_UPSTREAM_BODY_MAX_BYTES = 4096
_REQUEST_ID_HEADERS = ("x-request-id", "x-correlation-id", "request-id")
_MAX_FILE_BYTES = 1024 * 1024  # 1 MB decoded

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
    raw_type: str = ""


@dataclass(frozen=True)
class _ToolSpec:
    name: str
    description: str
    method: str
    path: str
    params: tuple[_ParamSpec, ...]
    system: str = ""
    base_url: str | None = None
    timeout: float | None = None
    static_headers: dict[str, str] = field(default_factory=dict)
    forward_headers: dict[str, str] = field(default_factory=dict)
    body_format: str = "json"
    file_param_names: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class _SystemSpec:
    """One yaml file = one system. Drives the per-system httpx.AsyncClient."""

    name: str
    base_url: str
    timeout: float
    max_connections: int
    default_headers: dict[str, str]
    source: Path
    verify_ssl: bool = True

    def make_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self.timeout,
            limits=httpx.Limits(max_connections=self.max_connections),
            headers=self.default_headers or None,
            verify=self.verify_ssl,
        )


@dataclass
class LoadFailure:
    source: str
    reason: str
    system: str = ""
    phase: str = "tool_parse"
    tool: str = ""


@dataclass
class LoadResult:
    tools: list[_ToolSpec]
    systems: dict[str, _SystemSpec]
    failures: list[LoadFailure]


@dataclass
class HubBundle:
    hub: FastMCP
    clients: dict[str, httpx.AsyncClient]
    failures: list[LoadFailure]
    tools: list[_ToolSpec] = field(default_factory=list)


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
        raw_type=type_key or "",
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

    raw_body_format = raw.get("body_format", "json")
    if raw_body_format not in _BODY_FORMATS:
        raise ValueError(
            f"tool {name!r}: body_format must be one of {sorted(_BODY_FORMATS)}, "
            f"got {raw_body_format!r}"
        )

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

    file_param_names: frozenset[str] = frozenset(p.name for p in params if p.raw_type == "file")

    # file params are only valid inside multipart body
    for p in params:
        if p.raw_type == "file":
            if p.location != "body":
                raise ValueError(
                    f"tool {name!r}: param {p.name!r} type 'file' must have location: body, "
                    f"got {p.location!r}"
                )
            if raw_body_format != "multipart":
                raise ValueError(
                    f"tool {name!r}: param {p.name!r} type 'file' requires "
                    f"body_format: multipart, got {raw_body_format!r}"
                )

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
        body_format=raw_body_format,
        file_param_names=file_param_names,
    )


_YAML_SUFFIXES = (".yaml",)
_DEFAULT_TIMEOUT = 30.0
_DEFAULT_MAX_CONNECTIONS = 100


def _parse_system_spec(doc: dict[str, Any], source: Path) -> _SystemSpec:
    defaults = doc.get("defaults") or {}
    raw_verify = defaults.get("verify_ssl", True)
    if not isinstance(raw_verify, bool):
        raise ValueError(
            f"{source}: defaults.verify_ssl must be a boolean (true/false), "
            f"got {raw_verify!r} ({type(raw_verify).__name__})"
        )
    return _SystemSpec(
        name=str(doc.get("system") or source.stem),
        base_url=str(defaults.get("base_url") or ""),
        timeout=float(defaults.get("timeout", _DEFAULT_TIMEOUT)),
        max_connections=int(defaults.get("max_connections", _DEFAULT_MAX_CONNECTIONS)),
        default_headers=_parse_headers(defaults.get("headers"), owner=f"{source} defaults.headers"),
        source=source,
        verify_ssl=raw_verify,
    )


def _record_failure(
    result: LoadResult,
    source: str,
    exc_or_msg: BaseException | str,
    *,
    strict: bool,
    system: str = "",
    phase: str = "tool_parse",
    tool: str = "",
) -> None:
    if strict:
        if isinstance(exc_or_msg, BaseException):
            raise exc_or_msg
        raise ValueError(exc_or_msg)
    result.failures.append(
        LoadFailure(
            source=source,
            reason=str(exc_or_msg),
            system=system,
            phase=phase,
            tool=tool,
        )
    )


def _load_one_file(
    source: Path,
    result: LoadResult,
    *,
    strict: bool,
    env: Mapping[str, str] | None = None,
) -> None:
    try:
        raw = source.read_text(encoding="utf-8")
    except OSError as exc:
        _record_failure(
            result, str(source), exc, strict=strict, system=source.stem, phase="file_parse"
        )
        return

    if env is not None:
        # KeyError from render_secrets means a secret is missing from the environment.
        # Let it propagate — a CrashLoop is visible; a literal placeholder reaching
        # upstream is not. This matches the render_secrets() module contract.
        raw = render_secrets(raw, env)

    try:
        doc = yaml.safe_load(raw) or {}
        if not isinstance(doc, dict):
            raise ValueError(f"top-level yaml must be a mapping, got {type(doc).__name__}")
        system = _parse_system_spec(doc, source)
    except (ValueError, yaml.YAMLError) as exc:
        _record_failure(
            result, str(source), exc, strict=strict, system=source.stem, phase="file_parse"
        )
        return

    if system.name in result.systems:
        _record_failure(
            result,
            str(source),
            f"duplicate system name {system.name!r}: also defined in "
            f"{result.systems[system.name].source}",
            strict=strict,
            system=system.name,
            phase="file_parse",
        )
        return

    seen_in_system: set[str] = set()
    for raw_tool in doc.get("tools") or []:
        raw_name = raw_tool.get("name") if isinstance(raw_tool, dict) else None
        try:
            tool = _parse_tool(raw_tool)
        except (TypeError, ValueError, KeyError) as exc:
            _record_failure(
                result,
                f"{source}:{raw_name or '?'}",
                exc,
                strict=strict,
                system=system.name,
                phase="tool_parse",
                tool=raw_name or "",
            )
            continue
        if tool.name in seen_in_system:
            _record_failure(
                result,
                f"{source}:{tool.name}",
                f"duplicate tool name {tool.name!r} within system {system.name!r}",
                strict=strict,
                system=system.name,
                phase="tool_parse",
                tool=tool.name,
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


def load_tools_yaml(
    path: str | Path,
    *,
    strict: bool = True,
    env: Mapping[str, str] | None = None,
) -> LoadResult:
    """Load a single yaml file OR a directory of yaml files.

    env controls secret rendering:
    - None  → skip rendering (doctor --placeholder-ok mode or tests without secrets)
    - dict  → render {% .Secrets.KEY %} from env; missing key raises KeyError (fail-fast)

    strict=True raises on first failure; strict=False collects and continues.
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
        result.failures.append(
            LoadFailure(source=str(p), reason=msg, system="unknown", phase="file_parse")
        )
        return result

    for fp in files:
        _load_one_file(fp, result, strict=strict, env=env)
    return result


def _build_signature(spec: _ToolSpec) -> inspect.Signature:
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
    """Substitute {header-name} placeholders from incoming (lowercased keys).

    Multiple placeholders in one template are all substituted. Returns None
    if any referenced header is absent — caller skips the outgoing header.
    """
    missing = False

    def _sub(match: re.Match[str]) -> str:
        nonlocal missing
        value = incoming.get(match.group(1))
        if value is None:
            missing = True
            return ""
        return value

    rendered = _TEMPLATE_PLACEHOLDER.sub(_sub, template)
    if missing or "{" in rendered:
        return None
    return rendered


def _make_tool_callable(
    spec: _ToolSpec,
    client: httpx.AsyncClient,
    base_url: str = "",
) -> Any:
    path_names: frozenset[str] = frozenset(p.name for p in spec.params if p.location == "path")
    query_names: frozenset[str] = frozenset(p.name for p in spec.params if p.location == "query")
    header_kebab: dict[str, str] = {
        p.name: p.name.replace("_", "-") for p in spec.params if p.location == "header"
    }
    body_names: frozenset[str] = frozenset(p.name for p in spec.params if p.location == "body")
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
            if name in path_names:
                path_args[name] = value
            elif name in query_names:
                if value is not None:
                    query[name] = value
            elif name in header_kebab:
                if value is not None:
                    headers[header_kebab[name]] = str(value)
            elif name in body_names and value is not None:
                body[name] = value

        rendered_path = spec.path.format(**path_args)
        if rendered_path.startswith(("http://", "https://")):
            url = rendered_path
        else:
            url = effective_base + rendered_path

        request_kwargs: dict[str, Any] = {}
        if query:
            request_kwargs["params"] = query
        if headers:
            request_kwargs["headers"] = headers

        if accepts_body and body:
            if spec.body_format == "form":
                request_kwargs["data"] = body
            elif spec.body_format == "multipart":
                # All fields go into `files=` so httpx always sends multipart/form-data.
                # Non-file fields use the (None, str(v)) tuple form (text part).
                # File fields are decoded from base64 bytes.
                multipart: dict[str, Any] = {}
                for k, v in body.items():
                    if k in spec.file_param_names and isinstance(v, str):
                        try:
                            decoded = base64.b64decode(v, validate=True)
                        except Exception as exc:
                            raise ToolError(
                                json.dumps(
                                    {"type": "invalid_base64", "param": k, "detail": str(exc)}
                                )
                            ) from exc
                        if len(decoded) > _MAX_FILE_BYTES:
                            raise ToolError(
                                json.dumps(
                                    {
                                        "type": "file_too_large",
                                        "param": k,
                                        "max_bytes": _MAX_FILE_BYTES,
                                        "received_bytes": len(decoded),
                                    }
                                )
                            )
                        multipart[k] = decoded
                    else:
                        if isinstance(v, (dict, list)):
                            multipart[k] = (None, json.dumps(v), "application/json")
                        else:
                            multipart[k] = (None, str(v))
                if multipart:
                    request_kwargs["files"] = multipart
            else:
                request_kwargs["json"] = body

        if spec.timeout is not None:
            request_kwargs["timeout"] = spec.timeout

        request_id = incoming.get("x-request-id")
        log_ctx = {"tool": spec.name, "system": spec.system, "request_id": request_id}
        start = time.perf_counter()

        try:
            resp = await client.request(spec.method, url, **request_kwargs)
        except httpx.TimeoutException as exc:
            duration = time.perf_counter() - start
            logger.error(
                "mcp_hub.timeout",
                latency_ms=int(duration * 1000),
                configured_timeout=spec.timeout,
                **log_ctx,
            )
            record_mcp_hub_tool_call(
                system=spec.system, tool=spec.name, outcome="timeout", duration_seconds=duration
            )
            raise ToolError(json.dumps({"type": _ERR_TIMEOUT, "message": str(exc)})) from exc
        except (httpx.ConnectError, httpx.ProxyError) as exc:
            duration = time.perf_counter() - start
            logger.error(
                "mcp_hub.connect_error",
                latency_ms=int(duration * 1000),
                error_type=type(exc).__name__,
                **log_ctx,
            )
            record_mcp_hub_tool_call(
                system=spec.system,
                tool=spec.name,
                outcome="connect_error",
                duration_seconds=duration,
            )
            raise ToolError(json.dumps({"type": _ERR_CONNECT, "message": str(exc)})) from exc

        duration = time.perf_counter() - start
        latency_ms = int(duration * 1000)
        upstream_request_id = _extract_request_id(resp.headers)

        if resp.status_code >= 500:
            logger.error(
                "mcp_hub.upstream_5xx",
                status=resp.status_code,
                latency_ms=latency_ms,
                upstream_request_id=upstream_request_id,
                **log_ctx,
            )
            record_mcp_hub_tool_call(
                system=spec.system,
                tool=spec.name,
                outcome="upstream_5xx",
                duration_seconds=duration,
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
            record_mcp_hub_tool_call(
                system=spec.system,
                tool=spec.name,
                outcome="upstream_4xx",
                duration_seconds=duration,
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
        record_mcp_hub_tool_call(
            system=spec.system, tool=spec.name, outcome="success", duration_seconds=duration
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


def build_hub(
    yaml_path: str | Path,
    *,
    name: str = "ragent-mcp-hub",
    env: Mapping[str, str] | None = None,
) -> HubBundle:
    """Construct a FastMCP server from a yaml file or directory of yaml files."""
    result = load_tools_yaml(yaml_path, strict=False, env=env)
    clients = {n: spec.make_client() for n, spec in result.systems.items()}

    mcp: FastMCP = FastMCP(name)
    registered = 0
    for spec in result.tools:
        client = clients.get(spec.system)
        if client is None:
            result.failures.append(
                LoadFailure(
                    source=spec.name,
                    reason=f"no client for system {spec.system!r}",
                    system=spec.system,
                    phase="registration",
                    tool=spec.name,
                )
            )
            continue
        try:
            fn = _make_tool_callable(spec, client)
            mcp.add_tool(fn)
            registered += 1
            record_mcp_hub_tool_registered(system=spec.system, tool=spec.name, method=spec.method)
        except Exception as exc:  # noqa: BLE001
            result.failures.append(
                LoadFailure(
                    source=spec.name,
                    reason=f"add_tool: {exc}",
                    system=spec.system,
                    phase="registration",
                    tool=spec.name,
                )
            )

    for failure in result.failures:
        logger.warning(
            "mcp_hub.load_failure",
            source=failure.source,
            reason=failure.reason,
            system=failure.system,
            phase=failure.phase,
            tool=failure.tool,
        )
        record_mcp_hub_load_failure(system=failure.system, phase=failure.phase)

    failed_systems = {f.system for f in result.failures if f.phase == "file_parse" and f.system}
    for sys_name, sys_spec in result.systems.items():
        record_mcp_hub_system_up(system=sys_name, up=True)
        logger.info(
            "mcp_hub.system_configured",
            system=sys_name,
            base_url=sys_spec.base_url,
            timeout=sys_spec.timeout,
            max_connections=sys_spec.max_connections,
            verify_ssl=sys_spec.verify_ssl,
        )
    for sys_name in failed_systems - result.systems.keys():
        record_mcp_hub_system_up(system=sys_name, up=False)
    logger.info(
        "mcp_hub.ready",
        systems=sorted(result.systems),
        tool_count=registered,
        failure_count=len(result.failures),
    )

    return HubBundle(hub=mcp, clients=clients, failures=result.failures, tools=result.tools)
