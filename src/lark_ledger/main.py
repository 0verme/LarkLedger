from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from lark_ledger import __version__
from lark_ledger.api import router
from lark_ledger.config import get_settings
from lark_ledger.db import SessionFactory, engine
from lark_ledger.services.ai import AIInterpreter
from lark_ledger.services.feishu import FeishuClient, MessageProcessor


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.processor = MessageProcessor(
        settings,
        SessionFactory,
        FeishuClient(settings),
        AIInterpreter(settings),
    )
    yield
    await engine.dispose()


app = FastAPI(
    title="LarkLedger / 飞账",
    description="Self-hosted AI bookkeeping bot for Feishu/Lark.",
    version=__version__,
    lifespan=lifespan,
)
app.include_router(router)
