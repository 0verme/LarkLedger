import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from lark_ledger import __version__
from lark_ledger.api import router
from lark_ledger.config import EventMode, get_settings
from lark_ledger.db import SessionFactory, engine
from lark_ledger.readiness import ReadinessService
from lark_ledger.services.ai import AIInterpreter
from lark_ledger.services.events import EventService
from lark_ledger.services.exchange import ExchangeRateService
from lark_ledger.services.feishu import FeishuClient, MessageProcessor
from lark_ledger.services.outbox import ReplyOutboxStore
from lark_ledger.services.reply_worker import ReplyDeliverer, ReplyWorker
from lark_ledger.services.websocket import LongConnectionReceiver
from lark_ledger.services.worker import EventWorker, EventWorkerStore, generate_owner_id


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.settings = settings
    app.state.shutting_down = False
    app.state.readiness = ReadinessService(settings, SessionFactory)
    if settings.event_mode is EventMode.WEBSOCKET and (
        not settings.lark_app_id or not settings.lark_app_secret
    ):
        raise RuntimeError(
            "websocket mode requires LARK_LEDGER_LARK_APP_ID and "
            "LARK_LEDGER_LARK_APP_SECRET"
        )
    reply_wakeup = asyncio.Event()
    processor = MessageProcessor(
        settings,
        SessionFactory,
        FeishuClient(settings),
        AIInterpreter(settings),
        exchange_rates=ExchangeRateService(settings),
        reply_worker_enabled=settings.reply_worker_enabled,
        wakeup=reply_wakeup.set,
    )
    event_service = EventService(
        SessionFactory, processor, worker_enabled=settings.worker_enabled
    )
    app.state.processor = processor
    app.state.event_service = event_service
    worker: EventWorker | None = None
    reply_worker: ReplyWorker | None = None
    receiver: LongConnectionReceiver | None = None
    try:
        if settings.worker_enabled:
            worker = EventWorker(
                EventWorkerStore(SessionFactory),
                processor,
                owner_id=generate_owner_id(),
                batch_size=settings.worker_batch_size,
                poll_interval_seconds=settings.worker_poll_interval_seconds,
                max_attempts=settings.event_max_attempts,
                lease_seconds=settings.event_lease_seconds,
                retry_base_seconds=settings.event_retry_base_seconds,
                retry_max_seconds=settings.event_retry_max_seconds,
            )
            app.state.event_worker = worker
            worker.start()
        if settings.reply_worker_enabled:
            reply_outbox_store = ReplyOutboxStore(SessionFactory)
            reply_worker = ReplyWorker(
                reply_outbox_store,
                ReplyDeliverer(
                    reply_outbox_store,
                    FeishuClient(settings),
                    owner_id=generate_owner_id(),
                    max_attempts=settings.reply_max_attempts,
                    retry_base_seconds=settings.reply_retry_base_seconds,
                    retry_max_seconds=settings.reply_retry_max_seconds,
                ),
                owner_id=generate_owner_id(),
                batch_size=settings.reply_worker_batch_size,
                poll_interval_seconds=settings.reply_worker_poll_interval_seconds,
                lease_seconds=settings.reply_lease_seconds,
                wakeup_event=reply_wakeup,
            )
            app.state.reply_worker = reply_worker
            reply_worker.start()
        if settings.event_mode is EventMode.WEBSOCKET:
            receiver = LongConnectionReceiver(settings, app.state.event_service)
            app.state.long_connection = receiver
            await receiver.start()
        yield
    finally:
        app.state.shutting_down = True
        # Stop accepting new events first, then the event worker (so in-flight
        # business commits finish or are left for a later reclaim), then the
        # reply worker (so no new outbox rows are claimed mid-shutdown), and
        # dispose the engine last. Rows are durable: anything undelivered is
        # picked up on the next start.
        if receiver is not None:
            await receiver.stop()
        if worker is not None:
            await worker.stop()
        if reply_worker is not None:
            await reply_worker.stop()
        await engine.dispose()


app = FastAPI(
    title="LarkLedger / 飞账",
    description="Self-hosted AI bookkeeping bot for Feishu/Lark.",
    version=__version__,
    lifespan=lifespan,
)
app.include_router(router)
