# Changelog

All notable changes to LarkLedger are documented in this file. The project follows [Semantic Versioning](https://semver.org/) while remaining in the `0.x` Alpha stage.

## [Unreleased]

### Added (v0.2.1 — transactional reply outbox; P06a)

- **Transactional Outbox:** new `reply_outbox` table (migration `20260806_0008`) stores durable, self-contained Feishu reply intents written in the **same transaction** as the ledger change they confirm. A successful business commit now guarantees a reply intent exists for later delivery or compensation.
- **Atomic business + reply:** `MessageProcessor` runs `LedgerService` with `commit_changes=False` (flush only), builds the reply intents from its result, and commits business + outbox together. Business failure and outbox failure roll back together; no business is ever committed without a matching outbox row.
- **Self-contained reply payloads:** text replies persist the final text verbatim; CSV exports and report images persist raw bytes in `payload_blob` (with `size` and `sha256`), so a later worker (P06b) can deliver after a container restart without re-calling AI, re-querying the ledger, or reopening a temporary file. `(event_id, reply_type)` is unique; `sequence` orders multi-message replies.
- **New `succeeded` semantics:** an event `succeeded` now means "business handled and reply intents durably written to the outbox", **not** "Feishu received the reply". Feishu send outcomes live on the outbox rows.
- **Crash-window recovery:** if business + outbox commit but the event status update is lost, the re-claimed event checks for an existing outbox row, skips business (no duplicate entries / replies), and converges to `succeeded` — the outbox pre-check replaces `IntegrityError → dead` as the normal recovery path.
- **Compatible post-commit single send:** after commit, the processor sends each committed intent once synchronously: success marks `sent`, failure marks `failed` (with a redacted, length-capped `result_summary`); a failed send never re-runs business and never fails the event. A failed CSV file send keeps the v0.2.0 direct fallback notice.
- **Outbox status enum:** `ReplyStatus` (`pending` / `sending` / `sent` / `failed` / `dead`) is centralized; P06a writes `pending` → `sent` | `failed` only, with `sending` and `dead` reserved for P06b. Status updates are conditional (`pending/sending/failed` only), so a `sent` row is never re-sent.

### Added (v0.2.1 — event worker, lease, retry, dead; P05b)

- Background PostgreSQL-driven **event worker** (`EventWorker`) started and stopped by the FastAPI lifespan. It claims `processed_events` rows in one transaction with `SELECT ... FOR UPDATE SKIP LOCKED`, writes `processing` / `lease_owner` / `lease_expires_at`, increments `attempt_count`, commits, then runs the `MessageProcessor` on the payload reloaded from the database. No Redis / Celery / RQ / Kafka / RabbitMQ; the database is the only queue.
- **Entry-point mode switch:** `LARK_LEDGER_WORKER_ENABLED` (default `true`) makes Webhook / WebSocket entries **claim only** and return immediately; processing moves to the worker. `false` restores the legacy in-process synchronous path. The two modes are mutually exclusive, so an event is never processed twice by both paths. Duplicate `event_id` still returns the dedup result immediately.
- **Lease semantics:** only the current `lease_owner` may write a processing outcome (guarded by `status='processing' AND lease_owner=<owner>`); an expired lease lets another worker reclaim the event (`attempt_count` increments again), and a stale worker can never overwrite the new owner's state. Lease duration defaults to 300 s; no renewal in this version.
- **Retry with exponential backoff:** retryable failures (network / timeout / 429 / 5xx / transient DB) are recorded as `failed` with `next_attempt_at = now + min(base × 2^(attempt-1), max)` (defaults 2 s / 3600 s) plus ~10% jitter. Unknown errors are conservatively retried.
- **Dead-lettering:** permanent errors (unparseable / unsupported-version payload, contract `ValueError` / `TypeError`, duplicate-constraint `IntegrityError`, non-429/408 4xx) or an exhausted attempt budget (default `event_max_attempts=3`, first attempt counts as 1) move the event to `dead`, clearing the lease and retaining the redacted error summary. No human replay in this version.
- **Crash recovery:** events left `processing` with an expired lease are reclaimed by another worker; a cancelled worker leaves its row for later takeover.
- **New settings:** `LARK_LEDGER_WORKER_ENABLED`, `WORKER_POLL_INTERVAL_SECONDS` (1.0), `WORKER_BATCH_SIZE` (10), `EVENT_MAX_ATTEMPTS` (3), `EVENT_LEASE_SECONDS` (300), `EVENT_RETRY_BASE_SECONDS` (2.0), `EVENT_RETRY_MAX_SECONDS` (3600).

### Added (v0.2.1 foundation — event state model only; P05a)

- Event state model foundation (P05a): `processed_events` gains `attempt_count`, `next_attempt_at`, `lease_owner`, `lease_expires_at`, `result_summary`, `source_message_id`, `user_open_id`, and `updated_at`; the `dead` terminal status is defined; migration `20260806_0007` backfills existing rows safely (already-processed rows get `attempt_count=1`, legacy payload-less rows stay non-replayable). Indexes support the worker's status/retry-window queries and operator lookups by source message or user.
- The sync path records `attempt_count` (each transition to `processing` counts one attempt) and a safe, length-capped, credential-redacted `result_summary`; successful events clear error fields.

### Changed

- Head migration is now `20260806_0008` (P06a adds the `reply_outbox` table).
- `LedgerService` gains `commit_changes: bool = True`; internal methods no longer commit, `execute()` commits once when enabled, and the batch-budget path uses savepoints. The Transactional Outbox path constructs the service with `commit_changes=False` so the processor owns the transaction.
- `EventService` gains a `claim()` (T1-only) path and routes `handle_safely` by worker mode; the synchronous `handle()` is retained for `WORKER_ENABLED=false` and tests. The `succeeded` status now means "business handled + reply intents written to the outbox".

### Security

- Event rows store only a single-line error summary with credentials (URL passwords, Authorization headers, Bearer tokens) redacted and a 512-character cap; full tracebacks are never persisted.
- Worker logs include `event_id`, `status`, `attempt_count`, a shortened owner label, retry time, and `error_code`, never the message body or payload.

### Known limitations (v0.2.1 is not finished)

- **Transactional Outbox is provided (P06a):** business changes and reply intents commit atomically, so a successful business write is always matched by a durable `reply_outbox` row and a crashed event converges to `succeeded` without re-running business.
- **Still missing (P06b):** a background reply worker, reply auto-retry, outbox lease / `FOR UPDATE SKIP LOCKED` claim loop, reply `dead` handling, user result replay, manual resend, and a complete readiness API. Today the processor sends each committed intent once synchronously; a failed send marks the outbox `failed` and waits for a future mechanism.
- **Pre-business error / notice replies** (e.g. "图片识别功能尚未配置", stage error prompts) are still sent directly and are **not** persisted to the outbox.
- CSV file send failures are recorded in the outbox and the v0.2.0 fallback notice is sent; they are not auto-retried in this version.
- The release must not claim "never double-bookkeeps"; the `(source_message_id, source_item_index)` unique constraint remains the fallback guard.

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
