# Windows / PowerShell equivalent of `make test-e2e-golden`.
#
# Runs the T7.3 retrieval-recall SLO against real third-party endpoints.
# Caller MUST set the live URLs + tokens first (env vars or
# parameters); the running_stack fixture defers to whatever values are
# already in the environment, falling back to WireMock only when
# absent.
#
# Usage:
#   $env:EMBEDDING_API_URL = "https://..."
#   $env:LLM_API_URL = "https://..."
#   ...
#   .\scripts\test-e2e-golden.ps1
#
# Or supply via parameters:
#   .\scripts\test-e2e-golden.ps1 `
#     -EmbeddingUrl "https://..." -LlmUrl "https://..." -RerankUrl "https://..." `
#     -EmbeddingToken "..." -LlmToken "..." -RerankToken "..."

[CmdletBinding()]
param(
    [string]$EmbeddingUrl   = $env:EMBEDDING_API_URL,
    [string]$LlmUrl         = $env:LLM_API_URL,
    [string]$RerankUrl      = $env:RERANK_API_URL,
    [string]$EmbeddingToken = $env:AI_EMBEDDING_API_J1_TOKEN,
    [string]$LlmToken       = $env:AI_LLM_API_J1_TOKEN,
    [string]$RerankToken    = $env:AI_RERANK_API_J1_TOKEN
)

$ErrorActionPreference = "Stop"

$required = @{
    "EMBEDDING_API_URL"           = $EmbeddingUrl
    "LLM_API_URL"                 = $LlmUrl
    "RERANK_API_URL"              = $RerankUrl
    "AI_EMBEDDING_API_J1_TOKEN"   = $EmbeddingToken
    "AI_LLM_API_J1_TOKEN"         = $LlmToken
    "AI_RERANK_API_J1_TOKEN"      = $RerankToken
}

$missing = $required.GetEnumerator() | Where-Object { [string]::IsNullOrWhiteSpace($_.Value) }
if ($missing) {
    Write-Host "ERROR: required env vars missing for T7.3 against real endpoints:" -ForegroundColor Red
    $missing | ForEach-Object { Write-Host "  - $($_.Key)" -ForegroundColor Red }
    Write-Host ""
    Write-Host "Export the six AI endpoint URLs + tokens, then re-run."
    exit 1
}

# Push values into the process env so `uv run pytest` (which uses subprocess
# inheritance) sees them. running_stack will preserve them via setdefault.
foreach ($e in $required.GetEnumerator()) {
    Set-Item "env:$($e.Key)" $e.Value
}
$env:RAGENT_E2E_GOLDEN_SET = "1"

# Sanity check Docker before pytest spends 30s booting containers only to
# crash on a missing daemon.
try {
    docker ps | Out-Null
} catch {
    Write-Host "ERROR: docker daemon not reachable — start Docker Desktop and retry." -ForegroundColor Red
    exit 1
}

uv run pytest `
    "tests/e2e/test_golden_set.py::test_golden_set_top3_accuracy_at_least_70pct" `
    -v --tb=short

exit $LASTEXITCODE
