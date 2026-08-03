from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from lark_ledger import __version__
from lark_ledger.api import router
from lark_ledger.config import EventMode, get_settings
from lark_ledger.db import SessionFactory, engine
from lark_ledger.services.ai import AIInterpreter
from lark_ledger.services.events import EventService
from lark_ledger.services.exchange import ExchangeRateService
from lark_ledger.services.feishu import FeishuClient, MessageProcessor
from lark_ledger.services.websocket import LongConnectionReceiver


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    if settings.event_mode is EventMode.WEBSOCKET and (
        not settings.lark_app_id or not settings.lark_app_secret
    ):
        raise RuntimeError(
            "websocket mode requires LARK_LEDGER_LARK_APP_ID and "
            "LARK_LEDGER_LARK_APP_SECRET"
        )
    processor = MessageProcessor(
        settings,
        SessionFactory,
        FeishuClient(settings),
        AIInterpreter(settings),
        exchange_rates=ExchangeRateService(settings),
    )
    app.state.processor = processor
    app.state.event_service = EventService(SessionFactory, processor)
    receiver: LongConnectionReceiver | None = None
    if settings.event_mode is EventMode.WEBSOCKET:
        receiver = LongConnectionReceiver(settings, app.state.event_service)
        app.state.long_connection = receiver
        await receiver.start()
    try:
        yield
    finally:
        if receiver is not None:
            await receiver.stop()
        await engine.dispose()


app = FastAPI(
    title="LarkLedger / 飞账",
    description="Self-hosted AI bookkeeping bot for Feishu/Lark.",
    version=__version__,
    lifespan=lifespan,
)
app.include_router(router)
