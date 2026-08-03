# Contributing to LarkLedger

[简体中文](CONTRIBUTING.md) | English

Thank you for improving LarkLedger. Discuss large features or behavior changes in an Issue first. Focused fixes and documentation improvements may be submitted directly as Pull Requests.

## Development environment

Use Python 3.11 or 3.12. The project uses a `src` layout, Alembic, async SQLAlchemy, and PostgreSQL in production. SQLite provides fast unit tests; changes to migrations, constraints, concurrency, or idempotency must also pass the PostgreSQL integration suite.

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"
cp .env.example .env
docker compose -f compose.yaml -f compose.dev.yaml up -d --build
```

Use a test Feishu/Lark application, test AI credentials, and a test database. Never connect development tools to a production ledger.

## Workflow

1. Fork the repository and create a focused branch from `main`.
2. Add tests for behavior changes and an Alembic migration for schema changes.
3. Update the affected README, environment guide, user guide, architecture, changelog, or upgrade notes.
4. Run the relevant checks:

```bash
ruff check .
mypy src
pytest --cov --cov-fail-under=88 -m "not postgres"
```

5. Describe the problem, implementation, validation, and any configuration, migration, security, or compatibility impact in the Pull Request.

CI runs Python 3.11/3.12 unit checks and a PostgreSQL 16 integration job.

## Documentation sources of truth

- User-visible commands, replies, and limits: `docs/help.md`
- Configuration defaults and explanations: `src/lark_ledger/config.py`, `docs/environment.md`, and `.env.example`
- Runtime, idempotency, and failure boundaries: `docs/architecture.md`
- Threat model and disclosure process: `SECURITY.md`
- Release and migration changes: `CHANGELOG.md` and `docs/upgrading.md`

Documentation is Chinese-first. The README and contribution guide are maintained in both languages. Use “WebSocket long-connection mode” and “Webhook mode” consistently.

## Design and privacy rules

- PostgreSQL is the ledger's source of truth; schema changes must be migratable.
- AI output is a minimal, strictly validated business action and never has database access.
- Every ledger, budget, report, correction, and undo operation must preserve user isolation.
- Both event transports must share source validation and `event_id` idempotency.
- Logs and exceptions must not expose message bodies, media, tokens, secrets, or database URLs.
- Tests and documentation must use entirely fictional or approved sanitized data.

By contributing, you agree that your work is licensed under [Apache License 2.0](LICENSE) and that project interactions follow the [Code of Conduct](CODE_OF_CONDUCT.md).
