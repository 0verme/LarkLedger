FROM node:24-alpine AS web-build

WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web ./
RUN npm run build

FROM python:3.11-slim AS runtime

# P42 build identity: injected by the release pipeline as Docker build args so
# the running image can answer "which build am I?" without a .git directory.
# The ARG declarations must live in this stage for ENV interpolation to work.
ARG LARK_LEDGER_VERSION=dev
ARG LARK_LEDGER_GIT_SHA=unknown
ARG LARK_LEDGER_BUILD_TIME=unknown

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    LARK_LEDGER_VERSION=$LARK_LEDGER_VERSION \
    LARK_LEDGER_GIT_SHA=$LARK_LEDGER_GIT_SHA \
    LARK_LEDGER_BUILD_TIME=$LARK_LEDGER_BUILD_TIME

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
RUN python -m venv /app/.venv
RUN pip install --no-cache-dir --upgrade pip setuptools
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY alembic.ini ./
COPY alembic ./alembic
COPY --from=web-build /web/dist ./web/dist

EXPOSE 8000

# P42 liveness: process-alive check only (no database, no external calls), so a
# container restart policy can react to a wedged process without being poisoned
# by business backlog. readiness is deliberately NOT the healthcheck: a 503 on
# /readyz means "not accepting traffic yet" and must not restart the container.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"

CMD ["uvicorn", "lark_ledger.main:app", "--host", "0.0.0.0", "--port", "8000"]
