# Changelog

All notable changes to LarkLedger are documented in this file. The project follows [Semantic Versioning](https://semver.org/) while remaining in the `0.x` Alpha stage.

## [Unreleased]

### Added (v0.2.1 foundation — event state model only)

- Event state model foundation (P05a): `processed_events` gains `attempt_count`, `next_attempt_at`, `lease_owner`, `lease_expires_at`, `result_summary`, `source_message_id`, `user_open_id`, and `updated_at`; the `dead` terminal status is defined; migration `20260806_0007` backfills existing rows safely (already-processed rows get `attempt_count=1`, legacy payload-less rows stay non-replayable). Indexes support the future worker's status/retry-window queries and operator lookups by source message or user.
- The sync path now records `attempt_count` (each transition to `processing` counts one attempt) and a safe, length-capped, credential-redacted `result_summary`; successful events clear error fields.
- **Scope note:** this is only the data-model groundwork for v0.2.1 reliable delivery. There is still **no Worker**, **no automatic retry or dead-letter processing**, **no Transactional Outbox**, and **no reply compensation**; claim-first behavior is unchanged.

### Changed

- Head migration is now `20260806_0007`.

### Security

- Event rows store only a single-line error summary with credentials (URL passwords, Authorization headers, Bearer tokens) redacted and a 512-character cap; full tracebacks are never persisted.

## [0.2.0] - 2026-08-05

Theme: **auditable ledger** — see entries, edit by short ID, soft-delete/restore, CSV export, formal Release/GHCR.

### Added

- Claim-time persistence of versioned, normalized Feishu event payloads on `processed_events` (payload envelope, transport, status, received_at) so future workers can replay without relying on in-memory events only.
- User-scoped five-character Crockford Base32 ledger `short_id` values (`#XXXXX` in chat replies), with migration backfill and unique constraint per user.
- Bot actions `list_entries` and `get_entry` for recent/filtered ledger lines and single-entry detail by short ID (keyset pagination via `查看 #XXXXX 之前的N笔`).
- Bot actions `update_entry`, `delete_entry`, and `restore_entry` for targeted short-ID mutations, with `ledger_entry_revisions` audit snapshots (also written by update-last / undo-last).
- Bot action `export_entries` for user-scoped CSV Schema v1 export (default last 90 days, optional full history / include deleted, 5000-row and 5MB caps, formula-injection hardening, Feishu file message delivery).
- Alembic migrations `20260805_0004` (event payload), `20260805_0005` (short ID), `20260805_0006` (revisions). Head: `20260805_0006`.
- WebSocket + text-only + PostgreSQL quickstart path in README / `.env.example` / deployment docs.

### Changed

- `EventService` documents and implements an explicit T1 claim / T2 sync-process boundary; duplicate `event_id` delivery behavior is unchanged (still claim-first, no automatic retry).
- Create / update-last / undo-last success messages include the affected entry short ID.
- Documentation and `.env.example` converge on a WebSocket + text-only + PostgreSQL quickstart; image/voice/Webhook remain documented extension paths. Runtime `Settings.event_mode` default remains `webhook` unless `.env` sets `websocket`.

### Security

- Event payloads may store message text and media resource identifiers; binaries, secrets, and signature headers are not stored. Logs continue to avoid dumping full payload bodies.
- CSV export hardens formula-injection risk on user-controlled text fields and never embeds `open_id` or internal UUIDs in the file.

### Known limitations (not fixed in 0.2.0)

- Still **claim-first**: failed events are not auto-retried; successful writes with failed replies are not auto-compensated.
- No pre-write confirmation for image / voice / batch.
- CSV file send failures are not auto-retried.
- No web admin UI, no shared ledgers; JSON export is not a formal capability.
- Planned themes without ship dates: **v0.2.1** reliable delivery; **v0.3.0** high-risk confirmation.

### Support

- Alpha release. Fixes target the latest release and `main`.
- Review the [upgrade guide](docs/upgrading.md) before changing deployed versions. Back up PostgreSQL before migrations.

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

[Unreleased]: https://github.com/0verme/LarkLedger/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/0verme/LarkLedger/releases/tag/v0.2.0
[0.1.0]: https://github.com/0verme/LarkLedger/releases/tag/v0.1.0
