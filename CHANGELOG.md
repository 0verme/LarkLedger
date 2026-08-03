# Changelog

All notable changes to LarkLedger are documented in this file. The project follows [Semantic Versioning](https://semver.org/) while remaining in the `0.x` Alpha stage.

## [Unreleased]

### Changed

- Nothing yet.

## [0.1.0] - 2026-08-03

### Added

- Self-hosted Feishu/Lark bookkeeping through text, voice, receipt images, payment-flow screenshots, and rich-text posts.
- Single-entry and batch bookkeeping, including up to 30 entries and 10 category budgets in one text request.
- Queries, last-entry correction and undo, monthly category budgets, threshold alerts, consumption report cards, and aggregated AI suggestions.
- Foreign-currency conversion with cached reference rates.
- WebSocket long-connection and Webhook event transports sharing `event_id` idempotency.
- PostgreSQL persistence, Alembic migrations, Docker Compose deployment, and multi-user isolation by Feishu `open_id`.

### Security

- AI providers receive only media or the minimum data required for parsing and aggregated advice; they have no database connection and cannot execute SQL.
- Webhook verification, encrypted-event handling, strict Pydantic schemas, parameterized queries, and guidance for secret handling are included.
- Runtime dependencies require a patched `cryptography` release and CI audits installed packages for known vulnerabilities.

### Support

- This is an Alpha release. Security and compatibility fixes are provided only for the latest release and `main`.
- Review the [upgrade guide](docs/upgrading.md) before changing deployed versions.

[Unreleased]: https://github.com/0verme/LarkLedger/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/0verme/LarkLedger/releases/tag/v0.1.0
