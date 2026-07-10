FROM python:3.12-slim

RUN pip install uv --no-cache-dir

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --no-cache --no-install-project

COPY src/ src/
RUN uv sync --no-dev --no-cache

RUN useradd -m -u 1000 mcp && chown -R mcp:mcp /app
USER mcp

ENTRYPOINT ["uv", "run", "python", "-m", "mcp_hub.server"]
