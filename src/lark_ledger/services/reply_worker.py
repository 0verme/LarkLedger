"""PostgreSQL-driven reply delivery worker with lease, retry, and dead-lettering.

P06b: a background ``ReplyWorker`` claims ``reply_outbox`` rows with
``SELECT ... FOR UPDATE SKIP LOCKED``, delivers each self-contained intent via
the ``ReplyDeliverer`` (text / file / card, uploading only what is not yet
uploaded), and records lease-guarded outcomes:

* ``sent`` — delivered successfully once; the Feishu ``message_id`` of the
  reply is persisted.
* ``failed`` — a retryable error; ``next_attempt_at`` is scheduled with
  exponential backoff so the row is picked up again later.
* ``dead`` — a permanent error (payload / contract / checksum / unsupported
  version or type) or the attempt budget is exhausted; the row is never
  claimed again.

The database is the only queue and coordination store. Concurrent workers in
other processes are safe because a claim is a single transaction that locks
rows with ``FOR UPDATE SKIP LOCKED`` and writes ``sending``, ``lease_owner``,
and ``lease_expires_at`` before committing. Outcome updates are guarded by
``status='sending' AND lease_owner=<owner>`` so a stale worker can never
overwrite a newer owner's state.

Event and reply concerns stay separated: the Event Worker only parses, does
business, and commits outbox intents; the Reply Worker only reads committed
outbox rows, uploads to Feishu, sends messages, and updates reply state. A
failed reply never re-executes business. Delivery uses the Feishu reply API's
``uuid`` idempotency key (the outbox row id), so a re-send after a crash closes
most of the "sent but not marked" duplicate window.

This module is shared with the compatible synchronous path
(``reply_worker_enabled=false``): the ``MessageProcessor`` claims each freshly
committed row with ``claim_by_id`` and runs the same ``ReplyDeliverer``, so no
send path bypasses the claim / lease / outcome guards.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
import sqlalchemy.exc

from lark_ledger.event_payload import safe_error_summary
from lark_ledger.outbox import (
    OUTBOX_PAYLOAD_VERSION,
    ReplyPayloadError,
    ReplyStatus,
    ReplyType,
    verify_blob_checksum,
)
from lark_ledger.services.errors import is_permanent_error
from lark_ledger.services.outbox import ClaimedReply, ReplyOutboxStore
from lark_ledger.services.worker import (
    failure_status,
    iso_datetime,
    safe_owner_id,
    schedule_next_attempt,
)

logger = logging.getLogger(__name__)

#: Worker loop name so tests can assert no dangling asyncio task survives.
WORKER_TASK_NAME = "lark-ledger-reply-worker"


class LeaseLostError(RuntimeError):
    """The worker lost its lease mid-delivery; another owner owns the row now."""


@dataclass(frozen=True)
class SendResult:
    """What a single delivery observed from Feishu (all fields optional)."""

    message_id: str | None = None
    file_key: str | None = None
    image_key: str | None = None


def is_permanent_reply_error(exc: BaseException) -> bool:
    """Decide whether a delivery failure is permanent (dead) or retryable.

    Permanent classes are explicit and small; anything not matched here falls
    back to ``is_permanent_error``'s conservative default (retryable), so
    transient network / HTTP 429 / 5xx / upload failures are retried:

    * ``ReplyPayloadError`` — the row cannot be delivered as stored (unsupported
      version / type, missing routing field, contract corruption, missing blob,
      checksum mismatch).
    * ``LeaseLostError`` — not a delivery failure at all; handled separately,
      never dead-lettered.
    * ``ValueError`` / ``TypeError`` — payload contract errors that the same
      data would reproduce forever.
    * ``IntegrityError`` — a constraint violation will not resolve on retry.
    * Non-408/429 4xx HTTP — explicit client / auth / permission errors.

    Unknown errors are retried conservatively up to ``reply_max_attempts`` and
    then moved to ``dead``.
    """
    if isinstance(exc, ReplyPayloadError):
        return True
    if isinstance(exc, LeaseLostError):
        return False
    if isinstance(exc, (ValueError, TypeError)):
        return True
    if isinstance(exc, sqlalchemy.exc.IntegrityError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return 400 <= code < 500 and code not in {408, 429}
    return is_permanent_error(exc)


class ReplyDeliverer:
    """Upload + send one claimed outbox row using only persisted data.

    Consumes only what the row carries: text is sent verbatim, a file is
    uploaded from ``payload_blob`` (reusing a persisted ``file_key``), and a
    card either reuses a persisted ``image_key`` or uploads the pre-rendered
    PNG and injects it. Nothing re-calls AI, re-queries the ledger, or reopens
    a temporary file. Every reply carries the outbox row id as Feishu's
    ``uuid`` idempotency key, so a retry after a "sent but not marked" crash is
    deduplicated by Feishu (1-hour window) instead of reaching the user twice.

    A report card whose image upload fails degrades to the stored text-only
    card (the image is optional enhancement); an export file is the whole
    point, so its upload failure is a normal retryable delivery failure.
    """

    def __init__(
        self,
        store: ReplyOutboxStore,
        feishu: Any,
        *,
        owner_id: str,
        max_attempts: int = 3,
        retry_base_seconds: float = 2.0,
        retry_max_seconds: float = 3600.0,
        jitter: Callable[[float], float] | None = None,
    ) -> None:
        self._store = store
        self._feishu = feishu
        self._owner_id = owner_id
        self._max_attempts = max_attempts
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds
        self._jitter = jitter

    @property
    def owner_id(self) -> str:
        return self._owner_id

    @property
    def max_attempts(self) -> int:
        return self._max_attempts

    @property
    def retry_base_seconds(self) -> float:
        return self._retry_base_seconds

    @property
    def retry_max_seconds(self) -> float:
        return self._retry_max_seconds

    async def process_item(self, item: ClaimedReply, now: datetime) -> str | None:
        """Deliver one claimed row and record the lease-guarded outcome.

        Returns the resulting status value (``sent`` / ``failed`` / ``dead``)
        or ``None`` when the lease was lost and the row must be left to its new
        owner. Never raises for delivery failures; a single bad row must not
        interrupt the worker sweep.
        """
        try:
            result = await self._send(item, now)
        except asyncio.CancelledError:
            # Leave the row sending+leased; another worker reclaims it after
            # the lease expires. This is the crash-recovery path.
            raise
        except LeaseLostError:
            logger.warning(
                "reply lease lost before send completed; leaving row to new owner "
                "outbox_id=%s event_id=%s reply_type=%s owner=%s",
                item.id,
                item.event_id,
                item.reply_type,
                safe_owner_id(self._owner_id),
            )
            return None
        except Exception as exc:
            return await self._record_failure(item, now, exc)
        recorded = await self._store.mark_sent(
            item.id,
            self._owner_id,
            now=now,
            remote_message_id=result.message_id,
        )
        if not recorded:
            logger.warning(
                "reply lease lost after send; not overwriting "
                "outbox_id=%s event_id=%s reply_type=%s owner=%s",
                item.id,
                item.event_id,
                item.reply_type,
                safe_owner_id(self._owner_id),
            )
            return None
        logger.info(
            "reply delivered outbox_id=%s event_id=%s reply_type=%s sequence=%d "
            "attempt=%d remote_message_id=%s",
            item.id,
            item.event_id,
            item.reply_type,
            item.sequence,
            item.attempt_count,
            result.message_id or "-",
        )
        return ReplyStatus.SENT.value

    async def _send(self, item: ClaimedReply, now: datetime) -> SendResult:
        if item.reply_type == ReplyType.DIRECT_CARD.value:
            return await self._send_direct_card(item, now)
        if not item.message_id:
            raise ReplyPayloadError("outbox row is missing message_id (routing field)")
        if item.payload_version != OUTBOX_PAYLOAD_VERSION:
            raise ReplyPayloadError(f"unsupported outbox payload_version: {item.payload_version}")
        payload = item.payload_json
        if item.reply_type == ReplyType.TEXT.value:
            text = payload.get("text")
            if not isinstance(text, str) or not text:
                raise ReplyPayloadError("text outbox row is missing a text payload")
            message_id = await self._feishu.reply_text(item.message_id, text, uuid=item.id.hex)
            return SendResult(message_id=message_id)
        if item.reply_type == ReplyType.FILE.value:
            file_meta = payload.get("file")
            if not isinstance(file_meta, dict):
                raise ReplyPayloadError("file outbox row is missing file metadata")
            verify_blob_checksum(item.payload_blob, file_meta)
            file_key = item.remote_file_key
            reused = file_key is not None
            if file_key is None:
                file_key = await self._feishu.upload_file(item.payload_blob, file_meta["filename"])
                if not await self._store.persist_file_key(
                    item.id, self._owner_id, file_key=file_key, now=now
                ):
                    raise LeaseLostError("lease lost while persisting file_key")
            message_id = await self._feishu.reply_file(item.message_id, file_key, uuid=item.id.hex)
            logger.info(
                "file reply sent outbox_id=%s reused_existing_key=%s file_key_len=%d",
                item.id,
                reused,
                len(file_key),
            )
            return SendResult(message_id=message_id, file_key=file_key)
        if item.reply_type == ReplyType.CARD.value:
            card = payload.get("card")
            if not isinstance(card, dict):
                raise ReplyPayloadError("card outbox row is missing card payload")
            card = dict(card)
            image_meta = payload.get("image")
            if image_meta and item.payload_blob is None:
                # The envelope promises an image but the blob is gone: this is a
                # permanent contract violation, not a degradable upload failure.
                raise ReplyPayloadError(
                    "card outbox row is missing payload_blob required by image metadata"
                )
            image_key: str | None = None
            if item.payload_blob is not None:
                verify_blob_checksum(item.payload_blob, image_meta)
                image_key = item.remote_image_key
                if image_key is None:
                    try:
                        image_key = await self._feishu.upload_image(item.payload_blob)
                    except Exception:
                        # A report card degrades to its stored text-only body;
                        # the image is an optional enhancement (P06a behavior).
                        logger.warning(
                            "report image upload failed; sending text-only card "
                            "outbox_id=%s event_id=%s",
                            item.id,
                            item.event_id,
                        )
                        image_key = None
                    else:
                        if not await self._store.persist_image_key(
                            item.id, self._owner_id, image_key=image_key, now=now
                        ):
                            raise LeaseLostError("lease lost while persisting image_key")
                if image_key is not None:
                    image_meta = image_meta or {}
                    card = card_with_image(
                        card,
                        image_key,
                        str(image_meta.get("alt") or ""),
                    )
            message_id = await self._feishu.reply_card(item.message_id, card, uuid=item.id.hex)
            return SendResult(message_id=message_id, image_key=image_key)
        raise ReplyPayloadError(f"unsupported reply_type: {item.reply_type}")

    async def _send_direct_card(self, item: ClaimedReply, now: datetime) -> SendResult:
        """Deliver a proactive card to a user's ``open_id`` (P29 reminders).

        The envelope carries the recipient and the card; the outbox routing
        field ``message_id`` is empty because there is no message to reply to.
        Idempotency comes from the same Feishu ``uuid`` key (the outbox row id).
        """
        if item.payload_version != OUTBOX_PAYLOAD_VERSION:
            raise ReplyPayloadError(f"unsupported outbox payload_version: {item.payload_version}")
        payload = item.payload_json
        open_id = payload.get("open_id")
        if not isinstance(open_id, str) or not open_id:
            raise ReplyPayloadError("direct_card outbox row is missing recipient open_id")
        card = payload.get("card")
        if not isinstance(card, dict):
            raise ReplyPayloadError("direct_card outbox row is missing card payload")
        message_id = await self._feishu.send_card(open_id, card, uuid=item.id.hex)
        return SendResult(message_id=message_id)

    async def _record_failure(
        self, item: ClaimedReply, now: datetime, exc: BaseException
    ) -> str | None:
        permanent = is_permanent_reply_error(exc)
        status = failure_status(
            item.attempt_count, max_attempts=self._max_attempts, permanent=permanent
        )
        next_attempt_at: datetime | None = None
        if status == ReplyStatus.FAILED.value:
            next_attempt_at = schedule_next_attempt(
                now,
                item.attempt_count,
                base_seconds=self._retry_base_seconds,
                max_seconds=self._retry_max_seconds,
                jitter=self._jitter,
            )
        error_code = type(exc).__name__
        recorded = await self._store.record_failure(
            item.id,
            self._owner_id,
            status=status,
            next_attempt_at=next_attempt_at,
            error_code=error_code,
            summary=safe_error_summary(exc),
            now=now,
        )
        if not recorded:
            logger.warning(
                "reply lease lost while recording failure; not overwriting "
                "outbox_id=%s event_id=%s owner=%s",
                item.id,
                item.event_id,
                safe_owner_id(self._owner_id),
            )
            return None
        if status == ReplyStatus.DEAD.value:
            logger.warning(
                "reply moved to dead outbox_id=%s event_id=%s reply_type=%s "
                "sequence=%d attempt=%d error_code=%s",
                item.id,
                item.event_id,
                item.reply_type,
                item.sequence,
                item.attempt_count,
                error_code,
            )
        else:
            logger.warning(
                "reply failed and will retry outbox_id=%s event_id=%s reply_type=%s "
                "sequence=%d attempt=%d error_code=%s next_attempt_at=%s",
                item.id,
                item.event_id,
                item.reply_type,
                item.sequence,
                item.attempt_count,
                error_code,
                next_attempt_at,
            )
        return status


def card_with_image(card: dict[str, Any], image_key: str, alt: str) -> dict[str, Any]:
    """Return the stored card with its pre-rendered image element injected.

    The stored card body is the text-only variant (message + advice); at send
    time the advice element is replaced by the uploaded image so the delivered
    card carries exactly the chart the sender rendered.
    """
    elements = card.get("body", {}).get("elements", [])
    message_element = elements[0] if elements else {"tag": "markdown", "content": ""}
    body = dict(card.get("body") or {})
    body["elements"] = [
        message_element,
        {
            "tag": "img",
            "img_key": image_key,
            "alt": {"tag": "plain_text", "content": alt},
            "scale_type": "fit_horizontal",
            "preview": True,
        },
    ]
    rebuilt = dict(card)
    rebuilt["body"] = body
    return rebuilt


class ReplyWorker:
    """Background task that repeatedly claims and delivers outbox replies.

    ``start`` creates the loop task; ``stop`` requests a stop, cancels the task,
    and waits for it to finish so no dangling task survives shutdown. Tests can
    inject ``clock``, ``sleeper``, ``owner_id``, the store, the deliverer, and a
    ``wakeup_event`` to avoid real time, real sleep, and real network.

    ``wakeup()`` sets an in-process ``asyncio.Event`` that shortens the idle
    sleep after a processor commits new outbox rows. The database polling
    remains the source of truth: a lost wakeup only delays delivery by at most
    one poll interval.
    """

    def __init__(
        self,
        store: ReplyOutboxStore,
        deliverer: ReplyDeliverer,
        *,
        owner_id: str,
        batch_size: int = 10,
        poll_interval_seconds: float = 1.0,
        lease_seconds: float = 300.0,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        wakeup_event: asyncio.Event | None = None,
    ) -> None:
        from lark_ledger.services.worker import default_clock

        if deliverer.owner_id != owner_id:
            raise ValueError("reply worker and deliverer must use the same owner_id")
        self._store = store
        self._deliverer = deliverer
        self._owner_id = owner_id
        self._batch_size = batch_size
        self._poll_interval_seconds = poll_interval_seconds
        self._lease_seconds = lease_seconds
        self._clock = clock or default_clock
        self._sleeper = sleeper
        self._wakeup_event = wakeup_event
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._started = False
        self._task_done = False
        self._task_exception_code: str | None = None
        # P42 worker observability: in-process loop heartbeat (same contract as
        # ``EventWorker``) so readiness and /ops/status can report staleness.
        self._last_sweep_at: datetime | None = None
        self._last_success_at: datetime | None = None
        self._last_error_at: datetime | None = None
        self._sweeps = 0
        self._processed = 0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def health_snapshot(self) -> dict[str, bool | str | int | None]:
        """Return a redacted, read-only task state for readiness."""
        return {
            "started": self._started,
            "running": self.running,
            "stopping": self._stop.is_set(),
            "task_done": self._task_done,
            "task_exception": self._task_exception_code is not None,
            "last_error_code": self._task_exception_code,
            "last_sweep_at": iso_datetime(self._last_sweep_at),
            "last_success_at": iso_datetime(self._last_success_at),
            "last_error_at": iso_datetime(self._last_error_at),
            "sweeps": self._sweeps,
            "processed": self._processed,
        }

    def wakeup(self) -> None:
        """Best-effort in-process signal that new outbox rows may exist."""
        event = self._wakeup_event
        if event is not None:
            event.set()

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            raise RuntimeError("reply worker already started")
        self._stop.clear()
        self._started = True
        self._task_done = False
        self._task_exception_code = None
        # Restart resets the heartbeat so stale timestamps from a previous run
        # can never make the new run look wedged.
        self._last_sweep_at = None
        self._last_success_at = None
        self._last_error_at = None
        self._sweeps = 0
        self._processed = 0
        self._task = asyncio.create_task(self._run_loop(), name=WORKER_TASK_NAME)
        self._task.add_done_callback(self._consume_task_result)

    def _consume_task_result(self, task: asyncio.Task[None]) -> None:
        self._task_done = True
        if task.cancelled():
            return
        try:
            task.result()
        except Exception as exc:
            self._task_exception_code = type(exc).__name__
            self._last_error_at = self._clock()
            logger.error(
                "reply worker task exited unexpectedly error_code=%s owner=%s",
                self._task_exception_code,
                safe_owner_id(self._owner_id),
            )

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        if task is None:
            return
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("reply worker task raised during shutdown")
        self._task = None

    async def _run_loop(self) -> None:
        logger.info(
            "reply worker started owner=%s poll_interval=%.1fs batch=%d lease=%.0fs "
            "max_attempts=%d retry_base=%.1fs retry_max=%.0fs",
            safe_owner_id(self._owner_id),
            self._poll_interval_seconds,
            self._batch_size,
            self._lease_seconds,
            self._deliverer.max_attempts,
            self._deliverer.retry_base_seconds,
            self._deliverer.retry_max_seconds,
        )
        try:
            while not self._stop.is_set():
                try:
                    await self.run_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Connection-level failures must not kill the worker loop.
                    self._last_error_at = self._clock()
                    logger.exception("reply worker sweep failed; will retry")
                self._last_sweep_at = self._clock()
                self._sweeps += 1
                if self._stop.is_set():
                    break
                await self._sleep_until_next_poll()
        finally:
            logger.info("reply worker stopped owner=%s", safe_owner_id(self._owner_id))

    async def _sleep_until_next_poll(self) -> None:
        if self._wakeup_event is not None:
            try:
                await asyncio.wait_for(
                    self._wakeup_event.wait(), timeout=self._poll_interval_seconds
                )
            except TimeoutError:
                pass
            finally:
                self._wakeup_event.clear()
        else:
            await self._sleeper(self._poll_interval_seconds)

    async def run_once(self, *, now: datetime | None = None) -> int:
        """Claim and deliver one sweep; returns the number of claimed rows."""
        if self._stop.is_set():
            return 0
        current = now or self._clock()
        claimed = await self._store.claim_batch(
            self._owner_id, current, self._batch_size, self._lease_seconds
        )
        for item in claimed:
            if self._stop.is_set():
                break
            await self._deliverer.process_item(item, current)
            self._last_success_at = current
            self._processed += 1
        return len(claimed)
