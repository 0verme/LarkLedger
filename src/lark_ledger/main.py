import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, Response

from lark_ledger import __version__
from lark_ledger.api import router
from lark_ledger.client_api import api_v1_router
from lark_ledger.client_api import router as client_router
from lark_ledger.config import EventMode, Settings, get_settings
from lark_ledger.dashboard_static import DashboardSecurityHeaders, DashboardStaticFiles
from lark_ledger.db import SessionFactory, engine
from lark_ledger.readiness import ReadinessService
from lark_ledger.services.ai import AIInterpreter
from lark_ledger.services.card_action import CardActionService
from lark_ledger.services.cleanup import (
    CleanupService,
    CleanupStore,
    CleanupWorker,
    RetentionPolicy,
)
from lark_ledger.services.events import EventService
from lark_ledger.services.exchange import ExchangeRateService
from lark_ledger.services.feishu import FeishuClient, MessageProcessor
from lark_ledger.services.ledger_authorization import LedgerAuthorizationError
from lark_ledger.services.outbox import ReplyOutboxStore
from lark_ledger.services.recurring_worker import RecurringWorker, RecurringWorkerStore
from lark_ledger.services.reply_worker import ReplyDeliverer, ReplyWorker
from lark_ledger.services.websocket import LongConnectionReceiver
from lark_ledger.services.worker import EventWorker, EventWorkerStore, generate_owner_id
from lark_ledger.web_api import router as web_router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.settings = settings
    app.state.session_factory = SessionFactory
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
    card_action_service = CardActionService(
        settings,
        processor._pending_store,
        processor.exchange_rates,
        processor._signal_or_deliver,
    )
    app.state.card_action_service = card_action_service
    worker: EventWorker | None = None
    reply_worker: ReplyWorker | None = None
    cleanup_worker: CleanupWorker | None = None
    recurring_worker: RecurringWorker | None = None
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
            reply_owner_id = generate_owner_id()
            reply_worker = ReplyWorker(
                reply_outbox_store,
                ReplyDeliverer(
                    reply_outbox_store,
                    FeishuClient(settings),
                    owner_id=reply_owner_id,
                    max_attempts=settings.reply_max_attempts,
                    retry_base_seconds=settings.reply_retry_base_seconds,
                    retry_max_seconds=settings.reply_retry_max_seconds,
                ),
                owner_id=reply_owner_id,
                batch_size=settings.reply_worker_batch_size,
                poll_interval_seconds=settings.reply_worker_poll_interval_seconds,
                lease_seconds=settings.reply_lease_seconds,
                wakeup_event=reply_wakeup,
            )
            app.state.reply_worker = reply_worker
            reply_worker.start()
        if settings.cleanup_enabled:
            cleanup_worker = CleanupWorker(
                CleanupService(
                    CleanupStore(SessionFactory),
                    RetentionPolicy(
                        event_succeeded_days=settings.event_succeeded_retention_days,
                        event_dead_days=settings.event_dead_retention_days,
                        outbox_sent_days=settings.outbox_sent_retention_days,
                        outbox_dead_days=settings.outbox_dead_retention_days,
                        pending_retention_days=settings.pending_retention_days,
                    ),
                    batch_size=settings.cleanup_batch_size,
                ),
                interval_seconds=settings.cleanup_interval_seconds,
            )
            app.state.cleanup_worker = cleanup_worker
            cleanup_worker.start()
        if settings.recurring_enabled:
            recurring_worker = RecurringWorker(
                RecurringWorkerStore(SessionFactory, settings),
                owner_id=generate_owner_id(),
                batch_size=settings.recurring_batch_size,
                poll_interval_seconds=settings.recurring_poll_interval_seconds,
                deliverer=processor._signal_or_deliver,
            )
            app.state.recurring_worker = recurring_worker
            recurring_worker.start()
        if settings.event_mode is EventMode.WEBSOCKET:
            receiver = LongConnectionReceiver(
                settings,
                app.state.event_service,
                card_action_service=card_action_service,
            )
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
        if cleanup_worker is not None:
            await cleanup_worker.stop()
        if recurring_worker is not None:
            await recurring_worker.stop()
        await engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    initial_settings = settings or get_settings()
    application = FastAPI(
        title="LarkLedger / 飞账",
        description="Self-hosted AI bookkeeping bot for Feishu/Lark.",
        version=__version__,
        lifespan=lifespan,
    )

    @application.middleware("http")
    async def attach_request_id(request: Request, call_next: Any) -> Response:
        if request.url.path.startswith("/api/v1/") or request.url.path.startswith(
            "/api/client/v1/"
        ):
            import uuid as _uuid

            request.state.request_id = _uuid.uuid4().hex[:16]
        else:
            request.state.request_id = ""
        response = await call_next(request)
        return cast(Response, response)

    application.include_router(router)
    application.include_router(client_router)
    application.include_router(api_v1_router)

    def custom_openapi() -> dict[str, Any]:
        """Standard OpenAPI plus a documented Bearer security scheme for the
        channel-neutral client API (``/api/v1`` and its legacy alias)."""
        if application.openapi_schema:
            return application.openapi_schema
        schema = get_openapi(
            title=application.title,
            version=application.version,
            description=application.description,
            routes=application.routes,
        )
        schema.setdefault("components", {}).setdefault("securitySchemes", {})
        schema["components"]["securitySchemes"]["clientBearer"] = {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "llv1_",
            "description": "Personal access token created in the Web dashboard; "
            "only a digest is stored server-side.",
        }
        for path, operations in schema.get("paths", {}).items():
            if path.startswith("/api/v1/") or path.startswith("/api/client/v1/"):
                for operation in operations.values():
                    operation["security"] = [{"clientBearer": []}]
        application.openapi_schema = schema
        return schema

    # FastAPI documents ``app.openapi = custom_openapi``; direct assignment
    # trips pyright's method-assign, setattr trips ruff B010 — use noqa.
    setattr(application, "openapi", custom_openapi)  # noqa: B010

    @application.exception_handler(HTTPException)
    async def client_http_error(request: Request, exc: HTTPException) -> Response:
        if not (
            request.url.path.startswith("/api/v1/")
            or request.url.path.startswith("/api/client/v1/")
        ):
            return await http_exception_handler(request, exc)
        detail: dict[str, Any] = exc.detail if isinstance(exc.detail, dict) else {}
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": detail.get("code", "temporary_failure"),
                    "message": detail.get("message", "request failed"),
                    "request_id": getattr(request.state, "request_id", ""),
                }
            },
            headers=exc.headers,
        )

    @application.exception_handler(RequestValidationError)
    async def client_validation_error(
        request: Request, exc: RequestValidationError
    ) -> Response:
        if not (
            request.url.path.startswith("/api/v1/")
            or request.url.path.startswith("/api/client/v1/")
        ):
            return await request_validation_exception_handler(request, exc)
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "request validation failed",
                    "request_id": getattr(request.state, "request_id", ""),
                }
            },
        )

    @application.exception_handler(LedgerAuthorizationError)
    async def ledger_authorization_error(
        request: Request, exc: LedgerAuthorizationError
    ) -> JSONResponse:
        del exc
        if request.url.path.startswith("/api/v1/") or request.url.path.startswith(
            "/api/client/v1/"
        ):
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "code": "resource_not_found",
                        "message": "resource not found",
                        "request_id": getattr(request.state, "request_id", ""),
                    }
                },
            )
        return JSONResponse(status_code=403, content={"detail": "permission denied"})
    if initial_settings.dashboard_enabled:
        application.include_router(web_router)
        application.add_middleware(
            DashboardSecurityHeaders, hsts=initial_settings.dashboard_cookie_secure
        )
        static_dir = Path("web/dist")
        if static_dir.is_dir():
            application.mount(
                "/",
                DashboardStaticFiles(directory=static_dir, html=True),
                name="dashboard",
            )
    return application


app = create_app()
