.PHONY: check format lint test test-gate test-e2e-golden bootstrap doctor mcp-hub-doctor

bootstrap:
	uv sync --extra dev

check: format lint mcp-hub-doctor test

mcp-hub-doctor:
	uv run python -m ragent.mcp_hub.doctor src/ragent/mcp_hub/tools.example.yaml

format:
	uv run ruff format .

lint:
	uv run ruff check . --fix

test:
	uv run pytest --cov=src/ragent --cov-branch --cov-fail-under=92

test-gate:
	uv run pytest --ignore=tests/e2e --cov=src/ragent --cov-branch --cov-fail-under=92

# Run the T7.3 retrieval-recall SLO against real third-party endpoints.
# Caller MUST export the live URLs + tokens first; the running_stack
# fixture defers to whatever EMBEDDING_API_URL / LLM_API_URL /
# RERANK_API_URL / AI_*_TOKEN values are already in env, falling back to
# WireMock only when absent. Without RAGENT_E2E_GOLDEN_SET=1 the test
# stays xfail(run=False) and contributes no signal.
#
# Required env (export before invoking):
#   EMBEDDING_API_URL, LLM_API_URL, RERANK_API_URL
#   AI_EMBEDDING_API_J1_TOKEN, AI_LLM_API_J1_TOKEN, AI_RERANK_API_J1_TOKEN
test-e2e-golden:
	@for v in EMBEDDING_API_URL LLM_API_URL RERANK_API_URL \
	         AI_EMBEDDING_API_J1_TOKEN AI_LLM_API_J1_TOKEN AI_RERANK_API_J1_TOKEN; do \
	  if [ -z "$${!v}" ]; then \
	    echo "ERROR: $$v is not set — required to run T7.3 against real endpoints."; \
	    echo "       Export the six AI endpoint URLs + tokens, then re-run."; \
	    exit 1; \
	  fi; \
	done
	RAGENT_E2E_GOLDEN_SET=1 uv run pytest \
	  tests/e2e/test_golden_set.py::test_golden_set_top3_accuracy_at_least_70pct \
	  -v --tb=short

# Pre-flight readiness check — env, datastores, AI endpoints, alembic head.
# Add PROBE_LIVE=1 to additionally hit /livez and /readyz on a running API.
doctor:
	@uv run --env-file .env python scripts/app_doctor.py $(if $(PROBE_LIVE),--probe-live,)
