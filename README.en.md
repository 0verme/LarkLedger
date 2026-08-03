# LarkLedger

[简体中文](README.md) | English

> A self-hosted AI bookkeeping bot for Feishu/Lark. Record, query, correct, undo, budget, and review expenses through text, voice, receipts, or payment screenshots.

[![CI](https://github.com/0verme/LarkLedger/actions/workflows/ci.yml/badge.svg)](https://github.com/0verme/LarkLedger/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

LarkLedger keeps the ledger in your own PostgreSQL database. AI providers only turn messages into strictly validated business actions. They never receive a database connection, cannot generate or execute SQL, and all reads and writes use predefined parameterized queries.

Detailed user, deployment, and architecture documentation is currently Chinese-first. This README and the [English contribution guide](CONTRIBUTING.en.md) provide the international entry points.

## Features

- Text, voice, receipt, payment-flow screenshot, and rich-text image bookkeeping
- Up to 30 entries and 10 category budgets in one text request
- Up to five images in one rich-text message and up to 30 transactions from payment screenshots
- Correct or undo the current user's latest active entry
- Reference-rate conversion for common foreign currencies
- Time-, direction-, and category-based summaries
- Monthly category budgets with one-time 80% and 100% alerts
- Consumption report cards with aggregate AI suggestions
- User isolation by Feishu `open_id` and event idempotency by `event_id`
- WebSocket long-connection and Webhook delivery modes

## Showcase

| Payment screenshot batch | Voice batch |
| --- | --- |
| ![Batch bookkeeping from a payment screenshot](docs/assets/batch-image-bookkeeping.png) | ![Batch bookkeeping from a voice message](docs/assets/voice-batch-bookkeeping.png) |
| Receipt recognition | Complex text batch |
| ![Bookkeeping from a receipt photo](docs/assets/receipt-bookkeeping.png) | ![Multiple income and expense entries from natural language](docs/assets/text-batch-bookkeeping.png) |

The screenshots use sanitized data approved for publication. Always treat the bot's confirmation reply as the authoritative result.

## Quick local trial

Python 3.11+ and Docker Compose are required. The development overlay starts PostgreSQL 16 together with the application:

```bash
git clone https://github.com/0verme/LarkLedger.git
cd LarkLedger
cp .env.example .env
docker compose -f compose.yaml -f compose.dev.yaml up -d --build
curl http://localhost:8000/healthz
```

On Windows PowerShell, use `Copy-Item .env.example .env`. Before starting, replace the Feishu/Lark application and text AI placeholders in `.env`. The bundled database credentials are for local development only.

## Production deployment

The recommended event transport is the WebSocket long-connection mode because it needs no public domain, HTTPS callback, or tunnel. Use an independently managed PostgreSQL database in production.

1. Copy `.env.example` to `.env` and set `LARK_LEDGER_EVENT_MODE=websocket`.
2. Configure the PostgreSQL URL, Lark App ID/Secret, and the AI provider keys you use.
3. In the Feishu/Lark developer console, enable the bot, select long-connection event delivery, subscribe to `im.message.receive_v1`, and publish the application version.
4. Start from source with `docker compose up -d --build`, or use the versioned GHCR image described below.

For the prebuilt image, migrations are an explicit deployment step:

```bash
export LARK_LEDGER_IMAGE_TAG=0.1.0
docker compose -f compose.image.yaml run --rm app alembic upgrade head
docker compose -f compose.image.yaml up -d
```

Images are published as `ghcr.io/0verme/larkledger:<version>`. Read the [Chinese environment guide](docs/environment.md) for the complete variable table, event-mode setup, and production checklist.

## Event transports

| Mode | Best for | Public HTTPS | Callback credentials |
| --- | --- | --- | --- |
| WebSocket long connection | Local hosts, NAS, home servers | No | App ID and App Secret |
| Webhook | Existing public ingress or platform deployments | Yes | Verification Token and preferably Encrypt Key |

Both transports use the same source validation, `event_id` claim, message parsing, ledger action, and reply pipeline.

## Security boundary

```text
Lark message -> source verification / event claim -> media download -> AI parsing
                                                            |
                                            strict Pydantic validation
                                                            |
                                      fixed actions -> SQLAlchemy -> PostgreSQL
```

AI output is limited to predefined actions. Report suggestions receive aggregate totals and trends, not entry notes or user identifiers. See the [security policy](SECURITY.md) and [Chinese architecture document](docs/architecture.md).

## Development

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
ruff check .
mypy src
pytest --cov --cov-fail-under=88 -m "not postgres"
```

PostgreSQL-specific migrations, constraints, and concurrency behavior are tested separately in CI. See [CONTRIBUTING.en.md](CONTRIBUTING.en.md).

## Documentation

- [Chinese user guide](docs/help.md)
- [Chinese environment and deployment guide](docs/environment.md)
- [Chinese architecture](docs/architecture.md)
- [Upgrade guide](docs/upgrading.md)
- [Changelog](CHANGELOG.md)
- [Security policy](SECURITY.md)

## Current limitations

- Search, arbitrary historical editing, custom categories, per-user timezone/currency, data export, shared ledgers, and undo restoration are not yet supported.
- Background processing runs in the web process and is not a durable queue.
- Currency conversion uses a current reference rate; summaries cannot switch display currency.
- Receipt and model outputs must be checked against the confirmation reply.

## License

[Apache License 2.0](LICENSE)
