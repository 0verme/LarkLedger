# LarkLedger

[简体中文](README.md) | English

> A self-hosted AI bookkeeping bot for Feishu/Lark. The ledger lives in your PostgreSQL database. Language models only turn messages into strictly validated business actions—never SQL, never a database connection.

[![CI](https://github.com/0verme/LarkLedger/actions/workflows/ci.yml/badge.svg)](https://github.com/0verme/LarkLedger/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

Detailed user, deployment, and architecture docs are **Chinese-first**. This README is the English entry point for the recommended path.

## What works in v0.7.0

- **Shared household ledger (v0.7.0)**: one family = one internal-user group + one dedicated shared ledger, bookkept together by real members:
  - **Payer attribution**: `created_by ≠ paid_by` with deterministic payer resolution by member alias / display name / open_id / UUID (`B 买菜120` → B pays); aliases are maintained by the household owner and spending aggregates by payer
  - **Household overview**: one deterministic “family home” view (period income / expense / net, budget progress, member contributions, top categories, upcoming recurring, recent transactions, account balances); Feishu `概览 / 家庭概览 / 家庭开销` and Web `/overview` share the same backend
  - **Account-level privacy**: an account can be `shared` (all members) or `private` (owner only); a private account's balance, entries, recurring rules, pendings, budget consumption and member stats are invisible to everyone else, while personal ledgers behave exactly as before
- **Recurring rules**: turn known future income / expense into deterministic rules (`每月8号房租3500` / `每年6月15日保险2000` / `每周健身房100`). When a rule comes due the Recurring Worker generates a frozen confirmation pending and proactively sends a Feishu reminder card; **a rule only posts after confirmation**. Rules and pendings never consume budget — only confirmed expenses do. Pause / resume / skip / disable are supported, edits affect only future periods, and the same rule + period can never produce two pendings or two transactions under concurrency / retry
- **Budget 2.0**: monthly total and per-category budgets keyed by explicit month, with plan-vs-actual, remaining, usage rate and over-limit status derived live; transfers never count, and delete / restore / revision all recompute from current facts
- **Ledger-scoped accounts and transfers**: entries bind to a ledger account (cash / asset / liability) with opening balances, rename / default / archive lifecycle; transfers stay outside income / expense stats; per-account balance and total assets / liabilities / net worth are available from both Feishu and Web
- **Text bookkeeping** with a user-scoped five-character short ID (`#XXXXX`) in success replies; simple single text entries still write straight through
- **Recent list / single-entry detail** (`最近10笔`, `查看 #XXXXX`)
- **Targeted update, soft-delete, and restore** by short ID (plus last-entry shortcuts)
- **CSV export** of the current user's ledger (Feishu file message; needs extra scopes)
- Summaries, monthly category budgets, consumption report cards
- **High-risk confirmation**: image / voice / batch / likely-duplicate writes first create a pending confirmation (`#C-XXXXX`) — confirm or cancel by text or card button; confirmation always uses the frozen parse result, never re-calls AI
- User isolation by Feishu `open_id` and claim-first `event_id` idempotency
- **Reliable delivery**: background Event / Reply Workers, transactional reply outbox, PostgreSQL lease and exponential-backoff retry, readiness probes, terminal retention cleanup, and guarded manual event replay
- **Web Dashboard**: Feishu OAuth, financial overview, ledger and revisions, pending confirmations, analytics, budgets, recurring rules, reports, CSV downloads, and an administrator reliability console
- Self-hosted stack: FastAPI, React / TypeScript / Vite, PostgreSQL, Docker Compose

Simple single text remains a direct write: `午饭32元` immediately creates the ledger entry. Image, voice, batch, and likely-duplicate writes follow `media → preview card → user confirmation → ledger`. Text fallbacks are `确认 #C-A83F2`, `取消 #C-A83F2`, and `查看待确认` (or `确认列表`).

## Web Dashboard

v0.6.0 keeps the optional Chinese-first Dashboard and adds account, transfer, and recurring-rule management pages for ledger management, frozen pending confirmations, analytics, budgets, reports, constrained CSV downloads, and administrator-only delivery operations. It uses the same service layer, revisions, Outbox, replay guards, PostgreSQL state, and `user_open_id` isolation as the Feishu bot.

The production image embeds the Vite build and serves it from FastAPI; Node.js is not needed at runtime. Enable it only behind HTTPS:

```dotenv
LARK_LEDGER_DASHBOARD_ENABLED=true
LARK_LEDGER_DASHBOARD_BASE_URL=https://ledger.example.com
LARK_LEDGER_DASHBOARD_SESSION_SECRET=replace-with-at-least-32-high-entropy-characters
LARK_LEDGER_DASHBOARD_ADMIN_OPEN_IDS=ou_xxx,ou_yyy
```

Register `https://ledger.example.com/api/web/v1/auth/callback` in the Feishu app and grant `auth:user.id:read`. Configure the reverse proxy to pass `X-Forwarded-Proto` and trust only its explicit address. When disabled, `/api/web/v1/*` and Dashboard static routes are absent, while the bot and workers remain unchanged. See the [Chinese deployment guide](docs/environment.md#web-dashboard可选).

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
- Administrators can use the Dashboard or the dry-run-by-default `python -m lark_ledger.admin replay-event` CLI to replay provably safe `dead` / `failed` events; only an explicit second confirmation or `--execute` reruns business, and every accepted replay writes an audit. Events with an Outbox, source ledger results, or unproven historical atomicity are refused. Result replay consumes only the existing Outbox and never reruns business.
- **High-risk confirmation (v0.3.0)**: image / voice / batch / likely-duplicate writes wait for a user `确认 #C-XXXXX` (or a card button) before writing. Confirmations expire (default 24h) and are per-user only; no multi-level approval or shared confirmation.
- **Privacy is account-level, not field-level**: a private account hides balance / entries / recurring rules / pendings / budget consumption / member stats, but never member identity, aliases, or the payer aggregation口径; there is no amount-threshold, category or per-field ACL
- The Dashboard has only `USER` and `ADMIN`; there is no enterprise multi-tenancy, organization tree, complex RBAC, or shared ledger
- **Not AA / Splitwise**: no splitting, settlement, debt relations or per-person breakdown; **not double-entry**: the sole-proprietor chart-of-accounts / vouchers / debit-credit domain stays a future track and accounting fields never leak into personal income/expense; **not business finance**: no audit trails, approval flows, multi-currency settlement or financial-reporting duties
- **JSON export is not a formal capability** (CSV only)

Future roadmap themes are outside this release commitment; v0.7.0 does not expand into a multi-tenant finance ERP.

Current release: **v0.6.0** (v0.7.0 household sharing and privacy are implemented but not yet released as an image). Prebuilt image: `ghcr.io/0verme/larkledger:0.6.0` (also `0.6` / `latest`). You can also build from source with `docker compose ... --build`.

```bash
export LARK_LEDGER_IMAGE_TAG=0.6.0
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
