# Third-Party API

> Split out of `docs/00_rule.md` for navigability. The anchor name **§Third-Party API**
> is preserved verbatim so existing journal rules and code comments that pin
> `rule.md §Third-Party API` JSON samples remain valid references.

This document records the exact request and response field names of every external
API ragent calls. Every upstream client (request shape, response parsing) and every
unit-test mock MUST be verified field-by-field against the samples below — mock
field drift hides contract drift inside the test layer (see `00_journal.md`
2026-05-11 QA row).

#### Embedding API

**Endpoint:** `EMBEDDING_API_URL` (default: `http://{embed_base_url}/text_embedding`)
**Timeout:** 60s | **Retry:** 3x @ 1.0s backoff

**Request:**
```json
{
  "texts": ["text1", "text2"],
  "model": "bge-m3",
  "encoding-format": "float"
}
```


** Response Format:**
```json
{
  "returnCode": 96200,
  "returnMessage": "success"
  "returnData": [
    {"index": 0, "embedding": [0.1, 0.2, ...]},
    {"index": 1, "embedding": [0.3, 0.4, ...]}
  ]
}
```

---

#### LLM API

**Endpoint:** `LLM_API_URL` (default: `http://{llm_base_url}/gpt_oss_120b/v1/chat/completions`)
**Timeout:** 120s | **Retry:** 3x @ 2.0s backoff

**Request:**
```json
{
  "model": "gptoss-120b",
  "messages": [
     {"role": "system", "content": "system prompt"},
     {"role": "user", "content": "user input"}
  ],
  "max_tokens": 4096,
  "stream": true,
  "temperature": 0.0
}
```

**Response:**
```json
{
  "model": "gptoss-120b",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "Under a moonlit sky, the silver‑mane unicorn whispered a lullaby of starlight, gently guiding the sleepy forest creatures into sweet dreams.",
                "reasoning_content": "The user wants a one-sentence bedtime story about a unicorn. Simple. Provide a single sentence. Maybe whimsical.",
                "tool_calls": []
            },
            "logprobs": null,
            "finish_reason": "stop",
            "stop_reason": null
}
```

#### Rerank API

**Endpoint:** `RERANK_API_URL` (default: `http://{rerank_url}`)
**Timeout:** 120s | **Retry:** 3x @ 2.0s backoff

**Request:**
```json
{
    "question": "What is TSMC?",
    "documents": ["PS5, or PlayStation 5, is a video ...", "TSMC stands for Taiwan Semiconductor …", "TSMC is headquartered ..."],
    "model": "bge-reranker-base",
    "top_k": 2
} 
```

**Response:**
```json
{
    "returnCode": 96200,
    "returnMessage": "success",
    "returnData": [
        {
            "score": 0.9999051094055176,
            "index": 1
        },
        {
            "score": 0.6183387041091919,
            "index": 2
        }
    ]
}
```

---

#### LLM & Embedding & Re-rank Auth API (Token Exchange)

**Endpoint:** `AI_API_AUTH_URL` (default: `http://{auth-service-url}/auth/api/accesstoken`)
**Timeout:** `AI_API_AUTH_TIMEOUT` (default: 10s) | **Retry:** 3x @ 1.0s backoff

Exchanges J1 tokens for J2 tokens. Supports two modes:
- **Local**: Uses configured J1 token from `AI_LLM_API_J1_TOKEN` or `AI_EMBEDDING_API_J1_TOKEN` or `AI_RERANK_API_J1_TOKEN`
- **Kubernetes**: Uses service account token from `/var/run/secrets/kubernetes.io/serviceaccount/token` when `AI_USE_K8S_SERVICE_ACCOUNT_TOKEN=true`

**Request:**
```json
{
  "key": "j1-token-value"
}
```

**Response:**
```json
{
  "token": "j2-token-value",
  "expiresAt": "2026-01-07T13:20:36Z"
}
```

The `TokenManager` caches J2 tokens and refreshes them 5 minutes before expiration.

---

#### Unprotect API

**Endpoint:** `UNPROTECT_API_URL` (full URL, no default — required when `UNPROTECT_ENABLED=true`)
**Timeout:** `UNPROTECT_TIMEOUT_SECONDS` (default: 30s) | **Retry:** none at client layer (worker retries the whole ingest task — `WORKER_MAX_ATTEMPTS`)
**Content-Type:** `multipart/form-data` (request) / `application/octet-stream` (response — raw binary, **not** JSON)

Decrypts/unprotects a single file before the ingest pipeline runs. Called from the worker at `src/ragent/workers/ingest.py` immediately after `MinIO.get_object`; the returned bytes replace `data` for the rest of the pipeline.

**Request headers:**
```
apikey: <UNPROTECT_APIKEY>          # raw JWT, NO "Bearer " prefix. Redacted in http.upstream_error logs.
Content-Type: multipart/form-data; boundary=<httpx-generated>
```

**Request body (multipart form fields — names are verbatim):**
| Field | Type | Value |
|---|---|---|
| `fileInput` | file part | filename = `documents.object_key`; payload = raw bytes from MinIO |
| `delegatedUser` | text part | `{X-User-Id}{UNPROTECT_DELEGATED_USER_SUFFIX}` — single concatenated string, **no separator** |

**Response (success):** HTTP 200, body = raw decrypted bytes. `httpx.Response.content` is returned directly to the pipeline. No JSON envelope, no `returnCode`/`returnData` wrapping.

**Response (failure):** any status ≥ 400 raises `httpx.HTTPStatusError` via `response.raise_for_status()`; the shared `http` client's `install_error_logging` emits one `http.upstream_error` record (request body truncated at `HTTP_ERROR_LOG_MAX_BYTES`, `apikey` header redacted). The worker propagates the error and the document goes through the standard retry → FAILED path.

> **Field-name pins (per 2026-05-11 journal rule):** `fileInput`, `delegatedUser`, `apikey` are the exact wire names — do not rename in client code or test mocks.


> **P2 API contracts** (HR API, OpenFGA API) are referenced in `docs/00_spec.md §3.5` (PermissionClient/OpenFGA) and `§4.5` (Third-Party Client Catalog). Verify request/response field names against those samples before implementing any P2 client — field-name drift is a runtime `KeyError` hidden by unit-test mocks.
