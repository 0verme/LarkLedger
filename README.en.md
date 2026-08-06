# LarkLedger

[简体中文](README.md) | English

> A self-hosted AI bookkeeping bot for Feishu/Lark. The ledger lives in your PostgreSQL database. Language models only turn messages into strictly validated business actions—never SQL, never a database connection.

[![CI](https://github.com/0verme/LarkLedger/actions/workflows/ci.yml/badge.svg)](https://github.com/0verme/LarkLedger/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

Detailed user, deployment, and architecture docs are **Chinese-first**. This README is the English entry point for the recommended path.

## What works in v0.3.0

- **Text bookkeeping** with a user-scoped five-character short ID (`#XXXXX`) in success replies; simple single text entries still write straight through
- **Recent list / single-entry detail** (`最近10笔`, `查看 #XXXXX`)
- **Targeted update, soft-delete, and restore** by short ID (plus last-entry shortcuts)
- **CSV export** of the current user's ledger (Feishu file message; needs extra scopes)
- Summaries, monthly category budgets, consumption report cards
- **High-risk confirmation**: image / voice / batch / likely-duplicate writes first create a pending confirmation (`#C-XXXXX`) — confirm or cancel by text or card button; confirmation always uses the frozen parse result, never re-calls AI
- User isolation by Feishu `open_id` and claim-first `event_id` idempotency
- **Reliable delivery**: background Event / Reply Workers, transactional reply outbox, PostgreSQL lease and exponential-backoff retry, readiness probes, terminal retention cleanup, and guarded manual event replay
- Self-hosted stack: FastAPI, PostgreSQL, Docker Compose

## Who it is for

Technical self-hosters who use Feishu heavily and want a **private** ledger. The first-run goal is one successful **text-only** entry—not a full multi-modal production rollout.

## Quick start (recommended)

**WebSocket long connection + text-only + PostgreSQL + Docker Compose.**

No public callback URL is required. The host must still make **outbound** HTTPS calls to Feishu and your text AI provider.

```bash
git clone https://github.com/0verme/LarkLedger.git
cd LarkLedger
cp .env.example .env
# On Windows PowerShell: Copy-Item .env.example .env
# Edit .env: App ID/Secret, text AI key, and keep EVENT_MODE=websocket
docker compose -f compose.yaml -f compose.dev.yaml up -d --build
curl http://127.0.0.1:8000/healthz
```

### Minimum environment variables (text-only)

```dotenv
LARK_LEDGER_EVENT_MODE=websocket
LARK_LEDGER_DATABASE_URL=postgresql+asyncpg://lark_ledger:change-me@db:5432/lark_ledger
LARK_LEDGER_LARK_APP_ID=cli_xxxxxxxxxxxxx
LARK_LEDGER_LARK_APP_SECRET=replace-me
LARK_LEDGER_AI_API_KEY=replace-me
LARK_LEDGER_AI_BASE_URL=https://api.deepseek.com
LARK_LEDGER_AI_MODEL=deepseek-v4-flash
```

Important:

- Set `LARK_LEDGER_EVENT_MODE=websocket` in `.env`. The **runtime code default remains `webhook`**; the example and docs recommend WebSocket for first deploy.
- With `compose.dev.yaml`, Compose overrides the database URL to the bundled Postgres service.
- Vision / transcription keys are **not** required for text-only bookkeeping.
- WebSocket mode does **not** need Verification Token or Encrypt Key.
- Safe defaults you usually leave alone: `TIMEZONE=Asia/Shanghai`, `CURRENCY=CNY`.

Full variable table, Feishu permission notes, Webhook, and troubleshooting: [Chinese environment guide](docs/environment.md).

### Feishu app (text-only)

1. Create an enterprise custom app and enable the bot.
2. Under Events, choose **long connection** (WebSocket)—do not set a request URL.
3. Subscribe to `im.message.receive_v1`.
4. Grant the minimum scopes needed to **receive and send messages** (confirm exact scope names in the Feishu console).
5. Publish an app version so the config takes effect.
6. Add the bot to a test chat; in groups, mention the bot.
7. Start the container first if the console needs to verify the long connection.

### First acceptance checks

Send in Feishu (replace `#XXXXX` with the short ID the bot actually returns):

1. `午饭32元` → success reply with `#XXXXX`
2. `最近10笔` → the new row appears
3. `查看 #XXXXX` → detail
4. `把 #XXXXX 改成35元` → update
5. `删除 #XXXXX` / `恢复 #XXXXX` → soft-delete and restore
6. `导出最近90天账单` → CSV file message (**requires Feishu file upload / file message scopes**; not claimed as verified end-to-end in a real tenant from this repository alone)

### Health and logs

```bash
docker compose -f compose.yaml -f compose.dev.yaml ps
docker compose -f compose.yaml -f compose.dev.yaml logs -f app
curl http://127.0.0.1:8000/healthz
curl -f http://127.0.0.1:8000/readyz
```

Service name: `app`. Host port: **8000**. Source Compose runs `alembic upgrade head` before Uvicorn.

Expected WebSocket health shape:

```json
{"status":"ok","event_mode":"websocket","long_connection":"connected"}
```

`/healthz` is a database-independent liveness probe. `/readyz` additionally
checks PostgreSQL, the current Alembic revision, enabled Event / Reply Workers,
and the receiver in WebSocket mode. It returns HTTP 503 when the instance cannot
accept work and never probes Feishu or AI.

### Existing PostgreSQL

Create a dedicated user/database, point `LARK_LEDGER_DATABASE_URL` at a host the **container** can reach (not `localhost` meaning the container itself), then:

```bash
docker compose up -d --build
```

SQL examples and URL encoding notes live in [docs/environment.md](docs/environment.md).

## Event transports

| Mode | Role | Public HTTPS | Extra credentials |
| --- | --- | --- | --- |
| **WebSocket (recommended first path)** | NAS, home server, private network | No | App ID / Secret |
| **Webhook (advanced alternative)** | Existing public ingress / reverse proxy | Yes | Verification Token; Encrypt Key recommended |

Webhook URL: `https://your-domain/webhooks/feishu`. Webhook remains a supported path; it is not deprecated.

## Known limitations

Do **not** describe this as "never loses messages / never double-bookkeeps":

- Failed events **are** automatically retried (exponential backoff, default max 3 attempts) and move to `dead` when exhausted or permanently broken; business writes and reply intents commit atomically through the **Transactional Outbox** (P06a), so a crash retry never re-runs business. We still do **not** claim "never double-bookkeeps"; the source-message uniqueness constraint remains the fallback guard.
- Failed replies **are** auto-retried by the background **Reply Worker** (P06b): committed outbox intents are claimed with `SELECT ... FOR UPDATE SKIP LOCKED`, delivered with a database lease, retried with exponential backoff, and dead-lettered after permanent errors or exhausted attempts. A failed reply never re-runs business, and pending / failed replies are re-delivered after a restart.
- Each reply carries a stable Feishu `uuid` idempotency key (the outbox row id): within the 1-hour dedup window a re-send after a crash is deduplicated by Feishu. In the extreme case (Feishu sent, local mark lost, and the re-send is more than 1 hour later) a duplicate reply may reach the user — it can **never** cause duplicate business execution or double bookkeeping.
- A lightweight Cleanup Worker deletes only terminal delivery records in bounded batches: successful events / sent replies default to 30 days, while dead records default to 90 days. Ledger entries and revisions are never deleted. Cleanup is not a database backup; review audit requirements before shortening retention.
- Operators can use the dry-run-by-default `python -m lark_ledger.admin replay-event` CLI to replay provably safe `dead` / `failed` events; only explicit `--execute` reruns business, and every accepted replay writes an audit. Events with an Outbox, source ledger results, or unproven historical atomicity are refused. Result replay remains internal.
- **High-risk confirmation (v0.3.0)**: image / voice / batch / likely-duplicate writes wait for a user `确认 #C-XXXXX` (or a card button) before writing. Confirmations expire (default 24h) and are per-user only; no multi-level approval or shared confirmation.
- No web admin UI, no shared ledgers
- **JSON export is not a formal capability** (CSV only)

Roadmap themes (no promised ship dates): `v0.3.x` maintenance and increments (shared ledgers / web admin are not currently promised).

Current release: **v0.2.1**. Prebuilt image: `ghcr.io/0verme/larkledger:0.2.1` (also `0.2` / `latest`). You can also build from source with `docker compose ... --build`.

```bash
export LARK_LEDGER_IMAGE_TAG=0.2.1
docker compose -f compose.image.yaml pull
docker compose -f compose.image.yaml run --rm app alembic upgrade head
docker compose -f compose.image.yaml up -d
```

## Development

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
ruff check .
mypy src
pytest --cov --cov-fail-under=88 -m "not postgres"
```

PostgreSQL-specific tests run in CI. See [CONTRIBUTING.en.md](CONTRIBUTING.en.md).

## Documentation

- [Chinese user guide](docs/help.md)
- [Chinese environment and deployment guide](docs/environment.md)
- [Chinese architecture](docs/architecture.md)
- [Upgrade guide](docs/upgrading.md)
- [Changelog](CHANGELOG.md)
- [Security policy](SECURITY.md)

## License

[Apache License 2.0](LICENSE)
