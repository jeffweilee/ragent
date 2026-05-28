### 3.9 MCP Hub Microservice

Standalone FastMCP-based service (`src/ragent/mcp_hub/`) that loads `tools.yaml` files at startup and dynamically registers each declared REST endpoint as an MCP tool. **Different scope** from §3.8: §3.8 is the in-process JSON-RPC server bolted onto the API exposing only `retrieve`; §3.9 is a separate microservice that federates arbitrary third-party REST APIs into one MCP surface for agent clients (Claude Desktop / Cursor / in-house). The Hub holds no upstream tokens — identity (`X-User-Id`, `Authorization`, etc.) is supplied by the MCP client per request and selectively forwarded by yaml.

#### 3.9.1 Process and transport

- **Entry point (primary):** `uvicorn ragent.mcp_hub.server:build_mcp_app --factory --host ${MCP_HUB_HOST:-0.0.0.0} --port ${MCP_HUB_PORT:-9000}`; `build_mcp_app()` is the 0-arg ASGI factory. Legacy shim `python -m ragent.mcp_hub.server` calls `main()` which delegates to the same factory.
- **Transport:** FastMCP Streamable HTTP, mounted at `MCP_HUB_PATH` (default `/mcp`). Clients connect to `http://<host>:<port>{MCP_HUB_PATH}/`.
- **Bind:** `MCP_HUB_HOST` (default `0.0.0.0`), `MCP_HUB_PORT` (default `9000`).
- **Registry source:** `MCP_HUB_TOOLS_YAML` (default `tools.yaml`); may be a single yaml file OR a directory. In directory mode every `*.yaml`/`*.yml` is one SYSTEM (name = filename stem, overridable via top-level `system:`); tool names auto-qualify as `<system>.<tool>` so independent registries can reuse raw names.
- **Per-system isolation:** each system gets its own `httpx.AsyncClient` (`defaults.timeout`, `defaults.max_connections`, `defaults.headers`). A slow upstream cannot starve other systems' pools.
- **Lifespan:** FastMCP's session-manager lifespan runs first; on shutdown the Hub closes every per-system httpx client. Wired through `server.build_app(bundle)` — production `main()` and the integration test share this factory.

#### 3.9.2 Env-var inventory

| Var | Default | Purpose |
|---|---|---|
| `MCP_HUB_TOOLS_YAML` | `tools.yaml` | File or directory; directory mode = multi-system |
| `MCP_HUB_NAME` | `ragent-mcp-hub` | Server name advertised in `initialize` |
| `MCP_HUB_HOST` | `0.0.0.0` | Bind host |
| `MCP_HUB_PORT` | `9000` | Bind port (must parse as int — non-numeric exits with `SystemExit`) |
| `MCP_HUB_PATH` | `/mcp` | Streamable HTTP mount path |

The Hub deliberately reads NO secrets/tokens from env — those flow via per-request MCP-client headers (see §3.9.4).

The Hub also serves `GET /metrics` (Prometheus exposition, sibling to `MCP_HUB_PATH`) — see §3.9.8.

#### 3.9.3 `tools.yaml` schema

```yaml
system: my-system           # optional; default = filename stem
defaults:
  base_url: https://api.example.com    # required if any tool path is relative
  timeout: 30.0                         # seconds; httpx default 30
  max_connections: 100                  # per-system pool size
  verify_ssl: true                      # MUST be an explicit yaml boolean (true/false).
                                        # Strings ("false", "true"), null, ints → load
                                        # failure. `mcp_hub.system_configured` log records
                                        # the value; false is for self-signed / staging.
  headers:                              # baseline headers on every request
    Accept: application/json

tools:
  - name: get_user                      # tool name (qualified as <system>.get_user)
    description: Fetch user by id.
    method: GET                         # GET/POST/PUT/PATCH/DELETE/HEAD
    path: /v1/users/{user_id}           # supports {placeholder} for path params
    timeout: 5.0                        # optional per-tool override
    base_url: https://other.example.com # optional per-tool override
    static_headers:                     # constant headers (literal strings only)
      X-Service: ragent
    forward_headers:                    # template per outgoing header
      Authorization: "Bearer {x-jwt-token}"
      X-User-Id: "{x-user-id}"
    parameters:
      - name: user_id
        type: string                    # string|integer|number|boolean|array|object
        location: path                  # path|query|body|header
        required: true
      - name: include_inactive
        type: boolean
        location: query
        required: false
        default: false
```

Validation (load-time, enforced by `doctor`):
- Duplicate tool names within a system → reject.
- `path` placeholders without a matching `location: path` param → reject.
- `location: path` params not referenced in the path template → reject.
- `location: body` params on a non-body method (GET/HEAD/DELETE) → reject.
- A header declared in both `static_headers` and `forward_headers` (case-insensitive) → reject.
- A `location: header` param colliding (after `_`→`-`, case-insensitive) with `static_headers`/`forward_headers` → reject (would silently fight at request time).
- Missing `defaults.base_url` when at least one tool path is relative → reject (absolute-URL tool paths still accepted).
- One bad yaml or one bad tool isolates: the rest of the registry still serves; failures surface on `HubBundle.failures` and as `mcp_hub.load_failure` warnings.

#### 3.9.4 Header forwarding contract

`HeaderForwardMiddleware` lowercases every incoming HTTP header and publishes the dict into a request-scoped `ContextVar` (`_INCOMING_HEADERS`). The Hub trusts these verbatim — deploy behind mTLS or a trusted internal network so untrusted callers cannot forge them. The MCP-client application sets them on its transport, out-of-band from the model loop; the LLM never controls header values.

Template syntax in `forward_headers` values:
- `{header-name}` placeholders reference incoming headers by lowercase name.
- Any missing placeholder → the entire outgoing header is skipped (graceful degradation; never sends empty strings).
- Composable: `Authorization: "Bearer {x-jwt-token}"`, `X-Trace: "user={x-user-id};req={x-request-id}"`.
- Outgoing header NAME on the left can be any case (HTTP is case-insensitive).

Merge order at request time: `system.defaults.headers` → tool `static_headers` (overrides) → rendered `forward_headers` (overrides) → `location: header` tool args (overrides).

#### 3.9.5 Response envelope

Every tool returns a discriminated dict so the LLM can branch on success/failure without parsing HTTP status:

```json
// 2xx
{"ok": true,  "status": 200, "data": <json-or-text-body>}
// 4xx — body preserved (JSON or text/plain, ≤ 4096 bytes, truncated flag if cut)
{"ok": false, "status": 404, "error": {"type": "upstream_4xx", "status": 404,
                                       "upstream_body": ..., "upstream_request_id": "..."}}
```

5xx, timeout, and connect errors raise `ToolError` (FastMCP propagates as JSON-RPC error). 5xx bodies are redacted (status + request_id only) to prevent stack-trace / SQL leakage. `x-request-id` from the upstream response is captured (also `x-correlation-id`, `request-id`).

#### 3.9.6 Operational tools

- `python -m ragent.mcp_hub.doctor` — CI-runnable yaml validator. Exit 0 on clean load, 1 on schema error, 2 on missing file. Reports ALL failures in one run (non-strict mode).
- Make target: `make mcp-hub-doctor` (chained into `make check`).

#### 3.9.7 Structured logging (operator-facing)

| Event | Level | Fields |
|---|---|---|
| `mcp_hub.system_configured` | INFO | `system`, `base_url`, `timeout`, `max_connections` |
| `mcp_hub.ready` | INFO | `systems`, `tool_count`, `failure_count` |
| `mcp_hub.load_failure` | WARN | `source`, `reason` |
| `mcp_hub.tool_call.success` | INFO | `tool`, `system`, `status`, `latency_ms`, `request_id` |
| `mcp_hub.upstream_4xx` | WARN | + `upstream_request_id` |
| `mcp_hub.upstream_5xx` | ERROR | + `upstream_request_id` |
| `mcp_hub.timeout` / `mcp_hub.connect_error` | ERROR | `tool`, `latency_ms`, `configured_timeout` |
| `mcp_hub.shutdown_error` | ERROR | `system`, `exc_info=True` |

SECURITY: rendered header VALUES (Authorization, JWT, API keys) are NEVER written to log output (test-pinned).

#### 3.9.8 Prometheus metrics (`GET /metrics`)

The Hub exposes Prometheus exposition on `GET /metrics` (sibling to `MCP_HUB_PATH`). `Route` (not `Mount`) so scrapers hit the canonical path without a 307 round-trip. Definitions live in `src/ragent/bootstrap/metrics.py` so registration is import-time singleton across processes.

| Metric | Type | Labels | Purpose |
|---|---|---|---|
| `mcp_hub_tool_load_failures_total` | Counter | `system`, `phase` ∈ {`file_parse`, `tool_parse`, `registration`} | Startup-time yaml / FastMCP registration failures. Phase distinguishes "bad file" vs "bad tool schema" vs "FastMCP rejected the registration". |
| `mcp_hub_tool_calls_total` | Counter | `system`, `tool`, `outcome` ∈ {`success`, `upstream_4xx`, `upstream_5xx`, `timeout`, `connect_error`} | Every tool invocation, terminal outcome. Drives per-tool error-rate alerts. |
| `mcp_hub_tool_call_duration_seconds` | Histogram | `system`, `outcome` | Upstream call latency. `tool` deliberately omitted to keep `_bucket` cardinality bounded; the counter above carries `tool` for drill-down. |

Cardinality: labels enumerate from closed enums (`phase`, `outcome`) or deployment-bounded sources (`system`, `tool` ← yaml-declared). Caller helpers (`record_mcp_hub_load_failure`, `record_mcp_hub_tool_call`) guard against caller typos by collapsing unknown enum values to a safe default — typos cannot blow up cardinality.

PromQL examples:
```promql
# Per-tool error rate (alert if > 0.05 sustained)
sum by (system, tool) (rate(mcp_hub_tool_calls_total{outcome!="success"}[5m]))
  / sum by (system, tool) (rate(mcp_hub_tool_calls_total[5m]))

# p95 latency per system
histogram_quantile(0.95,
  sum by (le, system) (rate(mcp_hub_tool_call_duration_seconds_bucket[5m])))

# Load-failure spike on a deploy
sum by (system, phase) (increase(mcp_hub_tool_load_failures_total[1h]))
```

Load failures appear simultaneously as structured `mcp_hub.load_failure` log events (fields: `source`, `reason`, `system`, `phase`, `tool`) so log-based debugging matches metric drill-down dimensions exactly.
