"""Operator-facing structured logging for the MCP Hub. Each tool call and
each load failure must surface enough fields for an operator to debug from
log lines alone."""

from __future__ import annotations

import logging

import httpx
import pytest
import structlog

from ragent.mcp_hub.mcp_hub import (
    _INCOMING_HEADERS,
    _make_tool_callable,
    _parse_tool,
    build_hub,
)


@pytest.fixture
def log_capture(caplog: pytest.LogCaptureFixture):
    """Configure structlog to emit through stdlib logging so caplog sees it."""
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
    caplog.set_level(logging.DEBUG)
    return caplog


def _spec_with_system(system: str, tool: str):
    return _parse_tool(
        {
            "name": tool,
            "method": "GET",
            "path": f"https://{system}.example.com/x",
        }
    ).__class__(
        name=f"{system}.{tool}",
        description="",
        method="GET",
        path=f"https://{system}.example.com/x",
        params=(),
        system=system,
    )


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_2xx_logs_tool_call_success(log_capture):
    spec = _spec_with_system("identity", "ping")

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    fn = _make_tool_callable(spec, _client(handler))
    await fn()

    records = [r for r in log_capture.records if "tool_call.success" in r.getMessage()]
    assert len(records) == 1
    msg = records[0].getMessage()
    assert '"system": "identity"' in msg
    assert '"tool": "identity.ping"' in msg
    assert '"status": 200' in msg
    assert '"latency_ms"' in msg


@pytest.mark.asyncio
async def test_4xx_logs_upstream_warning(log_capture):
    spec = _spec_with_system("billing", "get_charge")

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"err": "not found"})

    fn = _make_tool_callable(spec, _client(handler))
    await fn()

    records = [r for r in log_capture.records if "upstream_4xx" in r.getMessage()]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    msg = records[0].getMessage()
    assert '"status": 404' in msg
    assert '"system": "billing"' in msg


@pytest.mark.asyncio
async def test_5xx_logs_upstream_error(log_capture):
    from fastmcp.exceptions import ToolError

    spec = _spec_with_system("billing", "create")

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={})

    fn = _make_tool_callable(spec, _client(handler))
    with pytest.raises(ToolError):
        await fn()

    records = [r for r in log_capture.records if "upstream_5xx" in r.getMessage()]
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR


@pytest.mark.asyncio
async def test_timeout_logs_error(log_capture):
    from fastmcp.exceptions import ToolError

    spec = _spec_with_system("identity", "slow")

    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow")

    fn = _make_tool_callable(spec, _client(handler))
    with pytest.raises(ToolError):
        await fn()

    records = [r for r in log_capture.records if "mcp_hub.timeout" in r.getMessage()]
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR


@pytest.mark.asyncio
async def test_connect_error_logs_error(log_capture):
    from fastmcp.exceptions import ToolError

    spec = _spec_with_system("identity", "unreachable")

    def handler(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    fn = _make_tool_callable(spec, _client(handler))
    with pytest.raises(ToolError):
        await fn()

    records = [r for r in log_capture.records if "mcp_hub.connect_error" in r.getMessage()]
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert '"error_type": "ConnectError"' in records[0].getMessage()


def test_build_hub_logs_system_configured_per_system(log_capture, tmp_path):
    d = tmp_path / "tools.d"
    d.mkdir()
    (d / "alpha.yaml").write_text(
        "defaults:\n"
        "  base_url: https://alpha.example.com\n"
        "  timeout: 15.0\n"
        "  max_connections: 25\n"
        "tools:\n"
        "  - {name: p, method: GET, path: /p}\n"
    )

    build_hub(d, name="t")

    records = [r for r in log_capture.records if "mcp_hub.system_configured" in r.getMessage()]
    assert len(records) == 1
    msg = records[0].getMessage()
    assert '"system": "alpha"' in msg
    assert '"timeout": 15.0' in msg
    assert '"max_connections": 25' in msg


@pytest.mark.asyncio
async def test_request_id_propagated_into_log_fields(log_capture):
    spec = _spec_with_system("identity", "ping")

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    fn = _make_tool_callable(spec, _client(handler))
    token = _INCOMING_HEADERS.set({"x-request-id": "trc-abc"})
    try:
        await fn()
    finally:
        _INCOMING_HEADERS.reset(token)

    records = [r for r in log_capture.records if "tool_call.success" in r.getMessage()]
    assert '"request_id": "trc-abc"' in records[0].getMessage()


@pytest.mark.asyncio
async def test_header_values_never_appear_in_logs(log_capture, tmp_path):
    """The most important contract: rendered Authorization/JWT values MUST NOT
    appear in any log line."""
    spec = _parse_tool(
        {
            "name": "secure",
            "method": "GET",
            "path": "https://api.example.com/x",
            "static_headers": {"X-API-Key": "SECRET_LITERAL_VALUE"},
            "forward_headers": {"Authorization": "Bearer {x-jwt-token}"},
        }
    )
    spec_qualified = spec.__class__(
        name="api.secure",
        description="",
        method="GET",
        path=spec.path,
        params=(),
        system="api",
        static_headers=spec.static_headers,
        forward_headers=spec.forward_headers,
    )

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    fn = _make_tool_callable(spec_qualified, _client(handler))
    token = _INCOMING_HEADERS.set({"x-jwt-token": "eyJSECRETJWTabc"})
    try:
        await fn()
    finally:
        _INCOMING_HEADERS.reset(token)

    all_log_text = " ".join(r.getMessage() for r in log_capture.records)
    assert "SECRET_LITERAL_VALUE" not in all_log_text
    assert "eyJSECRETJWTabc" not in all_log_text


def test_build_hub_logs_ready_with_correct_tool_count(log_capture, tmp_path):
    d = tmp_path / "tools.d"
    d.mkdir()
    (d / "a.yaml").write_text(
        "tools:\n"
        "  - {name: ping1, method: GET, path: https://a.example.com/p}\n"
        "  - {name: ping2, method: GET, path: https://a.example.com/p}\n"
    )
    (d / "b.yaml").write_text(
        "tools:\n  - {name: ping3, method: GET, path: https://b.example.com/p}\n"
    )

    build_hub(d, name="t")

    records = [r for r in log_capture.records if "mcp_hub.ready" in r.getMessage()]
    assert len(records) == 1
    msg = records[0].getMessage()
    assert '"tool_count": 3' in msg
    assert '"failure_count": 0' in msg


def test_build_hub_logs_each_load_failure(log_capture, tmp_path):
    d = tmp_path / "tools.d"
    d.mkdir()
    (d / "ok.yaml").write_text(
        "tools:\n  - {name: ping, method: GET, path: https://ok.example.com/p}\n"
    )
    (d / "broken.yaml").write_text("[[[ broken yaml")

    build_hub(d, name="t")

    records = [r for r in log_capture.records if "load_failure" in r.getMessage()]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert "broken.yaml" in records[0].getMessage()
