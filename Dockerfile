FROM node:22-alpine AS frontend-builder
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.11-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    CRAWL4_AI_BASE_DIRECTORY=/app/data \
    SIMPLEX_DATA_DIR=/app/data \
    SEARXNG_URL=http://searxng:8080

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
      curl \
      git \
      socat \
      tesseract-ocr \
      tesseract-ocr-eng \
      tesseract-ocr-chi-tra \
      tesseract-ocr-chi-sim \
      tesseract-ocr-jpn \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements.lock ./
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.lock \
    && python -m playwright install --with-deps chromium \
    && python -m patchright install --with-deps chromium

COPY .env.example pyproject.toml ./
COPY *.py ./
COPY simplex_app ./simplex_app
COPY scripts/docker-entrypoint.sh ./scripts/docker-entrypoint.sh
COPY --from=frontend-builder /build/frontend/dist ./frontend/dist

RUN useradd --create-home --uid 10001 simplex \
    && mkdir -p /app/data \
    && chmod +x /app/scripts/docker-entrypoint.sh \
    && chown -R simplex:simplex /app /ms-playwright

USER simplex
EXPOSE 8788

HEALTHCHECK --interval=20s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8787/api/ready || exit 1

CMD ["/app/scripts/docker-entrypoint.sh"]
