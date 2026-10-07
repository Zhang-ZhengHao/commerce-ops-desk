# syntax=docker/dockerfile:1

FROM node:22-bookworm-slim AS frontend-build

WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build


FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    COMMERCE_OPS_DATABASE_URL=sqlite+pysqlite:////app/data/commerce_ops.db \
    COMMERCE_OPS_ENVIRONMENT=demo \
    COMMERCE_OPS_VENV_DIR=/opt/venv \
    PORT=8000

RUN groupadd --gid 10001 commerceops \
    && useradd --uid 10001 --gid 10001 --home-dir /app --shell /usr/sbin/nologin commerceops \
    && python -m venv /opt/venv

WORKDIR /app
COPY backend/requirements.lock /app/backend/requirements.lock
RUN /opt/venv/bin/python -m pip install \
    --no-cache-dir \
    --requirement /app/backend/requirements.lock

COPY --chown=commerceops:commerceops backend/ /app/backend/
COPY --chown=commerceops:commerceops scripts/ /app/scripts/
COPY --chown=commerceops:commerceops --from=frontend-build \
    /build/frontend/dist /app/frontend/dist

RUN mkdir -p /app/data \
    && chown commerceops:commerceops /app/data \
    && chmod 0700 /app/data

USER commerceops
EXPOSE 8000
VOLUME ["/app/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["/opt/venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).read()"]

CMD ["bash", "scripts/start-hosted.sh"]
