FROM python:3.12-slim

# Tesseract OCR — required by PyMuPDF for image-page text extraction.
# Languages: English, Chinese (Simplified + Traditional), Japanese, German.
# libgl1, libglib2.0-0: PyMuPDF runtime shared-library dependencies.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-chi-sim \
        tesseract-ocr-chi-tra \
        tesseract-ocr-jpn \
        tesseract-ocr-deu \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copy dependency manifests first to leverage Docker layer cache.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY src/ src/
COPY migrations/ migrations/
COPY alembic.ini ./
COPY resources/ resources/

ENV PYTHONUNBUFFERED=1

# Default: API process.
# Override CMD for other processes:
#   worker:     uv run python -m ragent.worker
#   reconciler: uv run python -m ragent.reconciler
CMD ["uv", "run", "python", "-m", "ragent.api"]
