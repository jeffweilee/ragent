.PHONY: check format lint test

check: format lint test

format:
	uv run ruff format .

lint:
	uv run ruff check . --fix

test:
	uv run pytest --cov=src/ragent --cov-branch --cov-fail-under=92
