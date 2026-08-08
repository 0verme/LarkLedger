FROM node:24-alpine AS web-build

WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web ./
RUN npm run build

FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

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
CMD ["uvicorn", "lark_ledger.main:app", "--host", "0.0.0.0", "--port", "8000"]
