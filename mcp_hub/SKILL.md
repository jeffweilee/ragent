# MCP Hub — Operator Onboarding Skill

Use this guide when adding a new upstream system API as an MCP tool, or
when modifying an existing tool YAML. No code changes are needed — only a
YAML file edit and a `git push`.

---

## Adding a New Upstream System

### 1. Create the YAML file

Each system gets **one file** in `deploy/helm/mcp-hub/files/tools.d/`:

```yaml
# system "payment" — example new system
system: payment

defaults:
  base_url: https://payment-api.internal
  timeout: 15.0
  max_connections: 20
  headers:
    Accept: application/json

tools:
  - name: charge
    description: Create a charge for a customer.
    method: POST
    path: /v1/charges
    static_headers:
      X-Api-Key: "{% .Secrets.PAYMENT_API_KEY %}"
    parameters:
      - name: amount
        type: integer
        location: body
        required: true
        description: Amount in cents.
      - name: currency
        type: string
        location: body
        required: false
        default: "usd"
```

**File naming**: `<system>.yaml`. The filename stem becomes the system name
unless overridden by the top-level `system:` key.

**Tool names** are auto-qualified as `<system>.<tool>` — `payment.charge`
in the example above.

---

### 2. Add secrets to Vault

For each `{% .Secrets.KEY %}` placeholder in the YAML, add the key to the
Vault KV-v2 path (`kv/data/mcp-hub/prod`) **and** to the VaultSecret CR at
`deploy/helm/mcp-hub/templates/vaultsecret.yaml`:

```yaml
  keys:
    - MCP_HUB_AUTH_TOKEN
    - PAYMENT_API_KEY   # ← add here
```

The VaultSecret operator syncs keys into the `mcp-hub-secrets` K8s Secret,
which the Pod consumes via `envFrom`. The hub reads them with `os.environ`
at startup via `render_secrets()`.

---

### 3. Validate in CI (no secrets needed)

```bash
cd services/mcp-hub
uv run mcp-hub-doctor path/to/tools.d --placeholder-ok
```

`--placeholder-ok` validates YAML structure and placeholder key format
(`[A-Z][A-Z0-9_]*`) without resolving real env vars. Run this in CI before
merging. Catches:
- Missing `base_url` for relative paths
- `{user_id}` in path with no matching `location: path` parameter
- Body parameters on GET/DELETE
- `{{` Helm syntax conflicts
- Lowercase or invalid secret key names

---

### 4. Push → ArgoCD syncs automatically

```bash
git add deploy/helm/mcp-hub/files/tools.d/payment.yaml
git add deploy/helm/mcp-hub/templates/vaultsecret.yaml
git commit -m "add payment system MCP tools"
git push
```

ArgoCD detects the ConfigMap change and applies it. **stakater/reloader**
restarts the mcp-hub Pod automatically (annotation `reloader.stakater.com/auto: "true"`
on the Deployment). **No image rebuild. No code change.**

---

## Parameter Reference

| Field | Values | Notes |
|-------|--------|-------|
| `method` | GET POST PUT PATCH DELETE | Uppercase |
| `location` | `body` `query` `path` `header` | Where the param goes in the request |
| `type` | `string` `integer` `number` `boolean` `array` `object` `file` | `file` = base64 string decoded to bytes; requires `body_format: multipart` |
| `body_format` | `json` (default) `form` `multipart` | Controls `Content-Type` of POST/PUT/PATCH body |
| `required` | `true` `false` | Optional params default to `null` unless `default:` is set |

## Header Reference

| Field | Example | Notes |
|-------|---------|-------|
| `static_headers` | `X-Api-Key: "{% .Secrets.KEY %}"` | Sent verbatim on every call; secrets injected from env |
| `forward_headers` | `X-User-Id: "{x-user-id}"` | Pulled from incoming MCP-client request; `{placeholder}` = lowercased header name |
| `forward_headers` multi-placeholder | `X-Ctx: "{x-user-id}@{x-tenant-id}"` | All placeholders must be present or the header is silently skipped |
| `defaults.headers` | `Accept: application/json` | Applied to every tool in the system; overridden by tool-level `static_headers` |

## Secrets Syntax

```
{% .Secrets.MY_KEY %}
```

- Key must match `[A-Z][A-Z0-9_]*` — uppercase, digits, underscores.
- Do **not** use `{{ }}` — Helm templates the file before the Pod sees it.
- Run `mcp-hub-doctor --placeholder-ok` in CI to catch bad key names early.

## Auth

The hub exposes a shared token guard on header `X-MCP-Hub-Token` (configurable
via `MCP_HUB_AUTH_HEADER`). MCP clients must include this header. The `/metrics`
endpoint is exempt for Prometheus scraping.

The auth header name **must not** appear in any tool's `forward_headers` — the
hub fails at startup with an error if it does (`_validate_auth_forward_conflict`).
