.PHONY: check format lint test test-gate bootstrap

# One-time dev provisioning: install host binaries the gate needs but `uv`
# can't ship. mysqldump is the canonical schema-drift differ; without it,
# tests/integration/test_schema_drift.py hard-fails by design (see the
# autouse _require_mysqldump fixture). Idempotent.
bootstrap:
	@if command -v mysqldump >/dev/null 2>&1; then \
	  echo "mysqldump present — skipping mariadb-client install."; \
	else \
	  echo "Installing mariadb-client (provides mysqldump for schema-drift gate)..."; \
	  sudo apt-get update -qq && sudo apt-get install -y mariadb-client; \
	fi
	uv sync --extra dev

check: format lint test

format:
	uv run ruff format .

lint:
	uv run ruff check . --fix

test:
	uv run pytest --cov=src/ragent --cov-branch --cov-fail-under=92

test-gate:
	uv run pytest --ignore=tests/e2e --cov=src/ragent --cov-branch --cov-fail-under=92
