# --- Stage 1: build the web UI (Vite writes it to /src/deep_agents_app/static/app, see ui/vite.config.ts) ---
FROM node:24-alpine AS ui
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY ui/ ./
RUN npm run build

# --- Stage 2: the Python service, with the built UI inside the package ---
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY --from=ui /src/deep_agents_app/static/app ./src/deep_agents_app/static/app
RUN pip install .

RUN useradd --system --create-home --uid 10001 agents \
    && mkdir -p /data \
    && chown agents:agents /data
USER agents
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "deep_agents_app.main:app", "--host", "0.0.0.0", "--port", "8000"]
