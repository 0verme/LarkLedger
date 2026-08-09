"""Authenticated API boundary for the optional Web Dashboard."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Annotated, cast
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lark_ledger import __version__
from lark_ledger.config import Settings
from lark_ledger.confirmation_id import ConfirmationCodeError, normalize_confirmation_code
from lark_ledger.models import Direction, Ledger
from lark_ledger.readiness import ReadinessService
from lark_ledger.schemas import Action, ParsedCommand, ReportData
from lark_ledger.services.dashboard_auth import (
    CSRF_COOKIE,
    CSRF_HEADER,
    OAUTH_COOKIE,
    SESSION_COOKIE,
    DashboardAuthError,
    DashboardAuthService,
    DashboardPrincipal,
)
from lark_ledger.services.event_replay import EventReplayService
from lark_ledger.services.ledger import EntryConflictError, LedgerService
from lark_ledger.services.ledger_management import (
    LedgerManagementError,
    LedgerManagementService,
    LedgerNameConflictError,
    LedgerNotFoundError,
)
from lark_ledger.services.pending import PendingCommandStore
from lark_ledger.services.replay import OutboxReplayService
from lark_ledger.services.web_admin import WebAdminQueryService
from lark_ledger.services.web_analytics import WebAnalyticsQueryService, local_date_bounds
from lark_ledger.services.web_ledger import WebLedgerQueryService
from lark_ledger.services.web_pending import WebPendingQueryService
from lark_ledger.web_schemas import (
    AdminDeadSummary,
    AdminEventPage,
    AdminEventStatus,
    AdminOutboxPage,
    AdminOutboxStatus,
    AnalyticsCategory,
    AnalyticsMonthlyPoint,
    AnalyticsOverview,
    AnalyticsPeriod,
    AnalyticsSummary,
    AnalyticsTrendPoint,
    BudgetOverview,
    BudgetUpdateRequest,
    DashboardData,
    DeletedFilter,
    EntryDetail,
    EntryPage,
    EntrySort,
    EntryUpdateRequest,
    EntryVersionRequest,
    EventReplayRequest,
    ExportRequestBody,
    LedgerList,
    LedgerNameRequest,
    PendingActionResponse,
    PendingDetail,
    PendingGroup,
    PendingPage,
    ResultReplayResponse,
    SafeSystemConfig,
    SortOrder,
    WebLedger,
)

router = APIRouter(prefix="/api/web/v1", tags=["web-dashboard"])


def _auth_service(request: Request) -> DashboardAuthService:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    return DashboardAuthService(settings, factory)


def _auth_error(exc: DashboardAuthError, code: int = 401) -> HTTPException:
    return HTTPException(status_code=code, detail=str(exc))


async def current_principal(
    service: Annotated[DashboardAuthService, Depends(_auth_service)],
    session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> DashboardPrincipal:
    try:
        return await service.authenticate(session_token)
    except DashboardAuthError as exc:
        raise _auth_error(exc) from exc


async def csrf_principal(
    service: Annotated[DashboardAuthService, Depends(_auth_service)],
    session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
    csrf_cookie: Annotated[str | None, Cookie(alias=CSRF_COOKIE)] = None,
    csrf_header: Annotated[str | None, Header(alias=CSRF_HEADER)] = None,
) -> DashboardPrincipal:
    try:
        return await service.verify_csrf(session_token, csrf_cookie, csrf_header)
    except DashboardAuthError as exc:
        raise _auth_error(exc, 403) from exc


async def admin_principal(
    principal: Annotated[DashboardPrincipal, Depends(current_principal)],
) -> DashboardPrincipal:
    if principal.role != "ADMIN":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return principal


@router.get("/auth/login")
async def login(
    request: Request,
    next_path: Annotated[str | None, Query(alias="next")] = None,
) -> RedirectResponse:
    service = _auth_service(request)
    try:
        oauth = service.begin_oauth(next_path)
    except DashboardAuthError as exc:
        raise _auth_error(exc, 400) from exc
    settings = cast(Settings, request.app.state.settings)
    response = RedirectResponse(oauth.authorize_url, status_code=302)
    response.set_cookie(
        OAUTH_COOKIE,
        oauth.state_cookie,
        max_age=settings.dashboard_oauth_state_ttl_seconds,
        httponly=True,
        secure=settings.dashboard_cookie_secure,
        samesite="lax",
        path="/api/web/v1/auth/callback",
    )
    return response


@router.get("/auth/callback")
async def callback(
    request: Request,
    code: str = "",
    state_value: Annotated[str, Query(alias="state")] = "",
    error: str = "",
    oauth_cookie: Annotated[str | None, Cookie(alias=OAUTH_COOKIE)] = None,
) -> RedirectResponse:
    service = _auth_service(request)
    try:
        verifier, next_path = service.complete_oauth_state(oauth_cookie, state_value)
        if error:
            raise DashboardAuthError("飞书登录已取消")
        identity = await service.exchange_identity(code, verifier)
        created = await service.create_session(identity)
    except DashboardAuthError as exc:
        raise _auth_error(exc) from exc
    settings = cast(Settings, request.app.state.settings)
    response = RedirectResponse(next_path, status_code=303)
    response.delete_cookie(
        OAUTH_COOKIE,
        path="/api/web/v1/auth/callback",
        secure=settings.dashboard_cookie_secure,
        httponly=True,
        samesite="lax",
    )
    response.set_cookie(
        SESSION_COOKIE,
        created.session_token,
        max_age=settings.dashboard_session_ttl_seconds,
        httponly=True,
        secure=settings.dashboard_cookie_secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE,
        created.csrf_token,
        max_age=settings.dashboard_session_ttl_seconds,
        httponly=False,
        secure=settings.dashboard_cookie_secure,
        samesite="lax",
        path="/",
    )
    return response


@router.post("/auth/logout", status_code=204)
async def logout(
    request: Request,
    _: Annotated[DashboardPrincipal, Depends(csrf_principal)],
    service: Annotated[DashboardAuthService, Depends(_auth_service)],
    session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> Response:
    await service.revoke(session_token)
    settings = cast(Settings, request.app.state.settings)
    response = Response(status_code=204)
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        secure=settings.dashboard_cookie_secure,
        httponly=True,
        samesite="lax",
    )
    response.delete_cookie(
        CSRF_COOKIE,
        path="/",
        secure=settings.dashboard_cookie_secure,
        httponly=False,
        samesite="lax",
    )
    return response


@router.get("/me")
async def me(
    principal: Annotated[DashboardPrincipal, Depends(current_principal)],
) -> dict[str, str]:
    return {
        "open_id": principal.user_open_id,
        "name": principal.display_name,
        "avatar_url": principal.avatar_url,
        "role": principal.role,
        "expires_at": principal.expires_at.isoformat(),
    }


def _web_ledger(ledger: Ledger, current_id: uuid.UUID) -> WebLedger:
    return WebLedger(
        id=str(ledger.id),
        name=ledger.name,
        is_default=ledger.is_default,
        is_current=ledger.id == current_id,
        currency=ledger.currency,
        timezone=ledger.timezone,
    )


def _ledger_http_error(exc: LedgerManagementError) -> HTTPException:
    if isinstance(exc, LedgerNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, LedgerNameConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


@router.get("/ledgers", response_model=LedgerList)
async def list_ledgers(
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(current_principal)],
) -> LedgerList:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        rows = await LedgerManagementService(
            session, currency=settings.currency, timezone=settings.timezone
        ).list_owned(principal.user_id)
        return LedgerList(items=[_web_ledger(row, principal.ledger_id) for row in rows])


@router.get("/ledgers/current", response_model=WebLedger)
async def current_ledger(
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(current_principal)],
) -> WebLedger:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        try:
            row = await LedgerManagementService(
                session, currency=settings.currency, timezone=settings.timezone
            ).get_owned(principal.user_id, principal.ledger_id)
        except LedgerManagementError as exc:
            raise _ledger_http_error(exc) from exc
        return _web_ledger(row, principal.ledger_id)


@router.post("/ledgers", response_model=WebLedger, status_code=201)
async def create_ledger(
    body: LedgerNameRequest,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
) -> WebLedger:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        try:
            row = await LedgerManagementService(
                session, currency=settings.currency, timezone=settings.timezone
            ).create(principal.user_id, body.name)
            await session.commit()
        except LedgerManagementError as exc:
            await session.rollback()
            raise _ledger_http_error(exc) from exc
        except IntegrityError as exc:
            await session.rollback()
            raise HTTPException(status_code=409, detail="已有同名或容易混淆的账本") from exc
        return _web_ledger(row, principal.ledger_id)


@router.post("/ledgers/{ledger_id}/select", response_model=WebLedger)
async def select_ledger(
    ledger_id: uuid.UUID,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
) -> WebLedger:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        try:
            row = await LedgerManagementService(
                session, currency=settings.currency, timezone=settings.timezone
            ).select_for_session(principal.user_id, uuid.UUID(principal.session_id), ledger_id)
            await session.commit()
        except LedgerManagementError as exc:
            await session.rollback()
            raise _ledger_http_error(exc) from exc
        return _web_ledger(row, row.id)


@router.patch("/ledgers/{ledger_id}", response_model=WebLedger)
async def rename_ledger(
    ledger_id: uuid.UUID,
    body: LedgerNameRequest,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
) -> WebLedger:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        try:
            row = await LedgerManagementService(
                session, currency=settings.currency, timezone=settings.timezone
            ).rename(principal.user_id, ledger_id, body.name)
            await session.commit()
        except LedgerManagementError as exc:
            await session.rollback()
            raise _ledger_http_error(exc) from exc
        except IntegrityError as exc:
            await session.rollback()
            raise HTTPException(status_code=409, detail="已有同名或容易混淆的账本") from exc
        return _web_ledger(row, principal.ledger_id)


@router.post("/ledgers/{ledger_id}/default", response_model=WebLedger)
async def set_default_ledger(
    ledger_id: uuid.UUID,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
) -> WebLedger:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        try:
            row = await LedgerManagementService(
                session, currency=settings.currency, timezone=settings.timezone
            ).set_default(principal.user_id, ledger_id)
            await session.commit()
        except LedgerManagementError as exc:
            await session.rollback()
            raise _ledger_http_error(exc) from exc
        return _web_ledger(row, principal.ledger_id)


@router.get("/dashboard", response_model=DashboardData)
async def dashboard(
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(current_principal)],
) -> DashboardData:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        return await WebLedgerQueryService(
            session, timezone=settings.timezone
        ).dashboard(principal.request_context)


@router.get("/entries", response_model=EntryPage)
async def entries(
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(current_principal)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
    start: datetime | None = None,
    end: datetime | None = None,
    direction: Direction | None = None,
    category: Annotated[str | None, Query(max_length=64)] = None,
    source_type: Annotated[str | None, Query(max_length=16)] = None,
    amount_min: Annotated[Decimal | None, Query(ge=0)] = None,
    amount_max: Annotated[Decimal | None, Query(ge=0)] = None,
    search: Annotated[str | None, Query(max_length=100)] = None,
    deleted: DeletedFilter = "active",
    sort: EntrySort = "occurred_at",
    order: SortOrder = "desc",
) -> EntryPage:
    if start is None:
        start = datetime.now(UTC) - timedelta(days=30)
    if end is not None and start >= end:
        raise HTTPException(status_code=422, detail="开始时间必须早于结束时间")
    if amount_min is not None and amount_max is not None and amount_min > amount_max:
        raise HTTPException(status_code=422, detail="最低金额不能大于最高金额")
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    settings = cast(Settings, request.app.state.settings)
    async with factory() as session:
        return await WebLedgerQueryService(
            session, timezone=settings.timezone
        ).list_entries(
            principal.request_context,
            page=page,
            page_size=page_size,
            start=start,
            end=end,
            direction=direction,
            category=category,
            source_type=source_type,
            amount_min=amount_min,
            amount_max=amount_max,
            search=search,
            deleted=deleted,
            sort=sort,
            order=order,
        )


@router.get("/entries/{short_id}", response_model=EntryDetail)
async def entry_detail(
    short_id: str,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(current_principal)],
) -> EntryDetail:
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    settings = cast(Settings, request.app.state.settings)
    async with factory() as session:
        try:
            detail = await WebLedgerQueryService(
                session, timezone=settings.timezone
            ).entry_detail(principal.request_context, short_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="账目不存在") from exc
    if detail is None:
        raise HTTPException(status_code=404, detail="账目不存在")
    return detail


async def _mutate_entry(
    *,
    request: Request,
    principal: DashboardPrincipal,
    short_id: str,
    command: ParsedCommand,
    expected_updated_at: datetime,
) -> EntryDetail:
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    settings = cast(Settings, request.app.state.settings)
    async with factory() as session:
        query = WebLedgerQueryService(session, timezone=settings.timezone)
        try:
            existing = await query.entry_detail(principal.request_context, short_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="账目不存在") from exc
        if existing is None:
            raise HTTPException(status_code=404, detail="账目不存在")
        try:
            await LedgerService(
                session,
                currency=settings.currency,
                timezone=settings.timezone,
            ).execute(
                principal.request_context,
                command,
                source_type="web",
                expected_updated_at=expected_updated_at,
            )
        except EntryConflictError as exc:
            await session.rollback()
            raise HTTPException(
                status_code=409, detail="账目已被其他请求修改，请刷新后重试"
            ) from exc
        refreshed = await query.entry_detail(principal.request_context, short_id)
        if refreshed is None:
            raise HTTPException(status_code=404, detail="账目不存在")
        return refreshed


@router.patch("/entries/{short_id}", response_model=EntryDetail)
async def update_entry(
    short_id: str,
    payload: EntryUpdateRequest,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
) -> EntryDetail:
    return await _mutate_entry(
        request=request,
        principal=principal,
        short_id=short_id,
        expected_updated_at=payload.expected_updated_at,
        command=ParsedCommand(
            action=Action.UPDATE_ENTRY,
            entry_ref=short_id,
            amount=payload.amount,
            direction=payload.direction,
            category=payload.category,
            note=payload.note,
            occurred_at=payload.occurred_at,
            clear_note=payload.note == "",
        ),
    )


@router.delete("/entries/{short_id}", response_model=EntryDetail)
async def delete_entry(
    short_id: str,
    payload: EntryVersionRequest,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
) -> EntryDetail:
    return await _mutate_entry(
        request=request,
        principal=principal,
        short_id=short_id,
        expected_updated_at=payload.expected_updated_at,
        command=ParsedCommand(action=Action.DELETE_ENTRY, entry_ref=short_id),
    )


@router.post("/entries/{short_id}/restore", response_model=EntryDetail)
async def restore_entry(
    short_id: str,
    payload: EntryVersionRequest,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
) -> EntryDetail:
    return await _mutate_entry(
        request=request,
        principal=principal,
        short_id=short_id,
        expected_updated_at=payload.expected_updated_at,
        command=ParsedCommand(action=Action.RESTORE_ENTRY, entry_ref=short_id),
    )


@router.get("/pending", response_model=PendingPage)
async def pending_list(
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(current_principal)],
    group: PendingGroup = "pending",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=50)] = 20,
) -> PendingPage:
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        return await WebPendingQueryService(session).list_pending(
            principal.request_context,
            group=group,
            page=page,
            page_size=page_size,
        )


@router.get("/pending/{confirmation_id}", response_model=PendingDetail)
async def pending_detail(
    confirmation_id: str,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(current_principal)],
) -> PendingDetail:
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        try:
            detail = await WebPendingQueryService(session).detail(
                principal.request_context, confirmation_id
            )
        except ConfirmationCodeError as exc:
            raise HTTPException(status_code=404, detail="确认单不存在") from exc
    if detail is None:
        raise HTTPException(status_code=404, detail="确认单不存在")
    return detail


async def _pending_action(
    *,
    action: str,
    confirmation_id: str,
    request: Request,
    principal: DashboardPrincipal,
) -> PendingActionResponse:
    try:
        code = normalize_confirmation_code(confirmation_id)
    except ConfirmationCodeError as exc:
        raise HTTPException(status_code=404, detail="确认单不存在") from exc
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    processor = getattr(request.app.state, "processor", None)
    store = cast(
        PendingCommandStore,
        getattr(processor, "_pending_store", None)
        or PendingCommandStore(factory, settings),
    )
    row = await store.get_by_code(principal.user_open_id, code)
    if row is None:
        raise HTTPException(status_code=404, detail="确认单不存在")
    if not row.source_message_id:
        raise HTTPException(status_code=409, detail="确认单缺少可靠回复目标")
    now = datetime.now(UTC)
    if action == "confirm":
        message, outbox = await store.confirm_and_execute(
            user_open_id=principal.user_open_id,
            confirmation_code=code,
            reply_to_message_id=row.source_message_id,
            confirm_event_id=None,
            exchange_rates=getattr(processor, "exchange_rates", None),
            now=now,
        )
    else:
        message, outbox = await store.cancel(
            user_open_id=principal.user_open_id,
            confirmation_code=code,
            reply_to_message_id=row.source_message_id,
            cancel_event_id=None,
            now=now,
        )
    if processor is not None:
        await processor._signal_or_deliver(outbox)
    async with factory() as session:
        detail = await WebPendingQueryService(session).detail(
            principal.request_context, code, now=now
        )
    if detail is None:
        raise HTTPException(status_code=404, detail="确认单不存在")
    expected = "executed" if action == "confirm" else "cancelled"
    if detail.pending.status != expected:
        raise HTTPException(status_code=409, detail=message)
    return PendingActionResponse(message=message, pending=detail)


@router.post("/pending/{confirmation_id}/confirm", response_model=PendingActionResponse)
async def confirm_pending(
    confirmation_id: str,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
) -> PendingActionResponse:
    return await _pending_action(
        action="confirm",
        confirmation_id=confirmation_id,
        request=request,
        principal=principal,
    )


@router.post("/pending/{confirmation_id}/cancel", response_model=PendingActionResponse)
async def cancel_pending(
    confirmation_id: str,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
) -> PendingActionResponse:
    return await _pending_action(
        action="cancel",
        confirmation_id=confirmation_id,
        request=request,
        principal=principal,
    )


@router.get("/admin/events", response_model=AdminEventPage)
async def admin_events(
    request: Request,
    _: Annotated[DashboardPrincipal, Depends(admin_principal)],
    event_status: Annotated[AdminEventStatus | None, Query(alias="status")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> AdminEventPage:
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        return await WebAdminQueryService(session).events(
            status=event_status, page=page, page_size=page_size
        )


@router.get("/admin/outbox", response_model=AdminOutboxPage)
async def admin_outbox(
    request: Request,
    _: Annotated[DashboardPrincipal, Depends(admin_principal)],
    outbox_status: Annotated[AdminOutboxStatus | None, Query(alias="status")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> AdminOutboxPage:
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        return await WebAdminQueryService(session).outbox(
            status=outbox_status, page=page, page_size=page_size
        )


@router.get("/admin/dead", response_model=AdminDeadSummary)
async def admin_dead(
    request: Request,
    _: Annotated[DashboardPrincipal, Depends(admin_principal)],
) -> AdminDeadSummary:
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        return await WebAdminQueryService(session).dead_summary()


@router.post(
    "/admin/outbox/{outbox_id}/replay", response_model=ResultReplayResponse
)
async def replay_outbox_result(
    outbox_id: uuid.UUID,
    request: Request,
    _: Annotated[DashboardPrincipal, Depends(csrf_principal)],
    admin: Annotated[DashboardPrincipal, Depends(admin_principal)],
) -> ResultReplayResponse:
    del admin
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    result = await OutboxReplayService(factory).replay_ids([outbox_id])
    if result.not_found:
        raise HTTPException(status_code=404, detail="回复记录不存在")
    if result.reset == 0:
        raise HTTPException(status_code=409, detail="当前回复状态不可重发")
    reply_worker = getattr(request.app.state, "reply_worker", None)
    if reply_worker is not None:
        reply_worker.wakeup()
    return ResultReplayResponse(
        reset=result.reset, skipped=result.skipped, not_found=result.not_found
    )


@router.post("/admin/events/{event_id}/replay")
async def replay_event(
    event_id: str,
    payload: EventReplayRequest,
    request: Request,
    _: Annotated[DashboardPrincipal, Depends(csrf_principal)],
    admin: Annotated[DashboardPrincipal, Depends(admin_principal)],
) -> dict[str, object]:
    if payload.execute and payload.confirmation_event_id != event_id:
        raise HTTPException(status_code=422, detail="二次确认事件 ID 不匹配")
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    try:
        result = await EventReplayService(factory).replay(
            event_id,
            operator=admin.user_open_id,
            reason=payload.reason,
            execute=payload.execute,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if payload.execute and result.outcome != "requeued":
        raise HTTPException(
            status_code=409,
            detail="安全预检未通过，事件状态已变化，请重新 Dry Run",
        )
    return result.to_safe_dict()


@router.get("/admin/health")
async def admin_health(
    request: Request,
    _: Annotated[DashboardPrincipal, Depends(admin_principal)],
) -> dict[str, object]:
    service = cast(ReadinessService | None, getattr(request.app.state, "readiness", None))
    if service is None:
        raise HTTPException(status_code=503, detail="系统尚未完成启动")
    return cast(dict[str, object], await service.check(request.app.state))


@router.get("/admin/config", response_model=SafeSystemConfig)
async def admin_config(
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(admin_principal)],
) -> SafeSystemConfig:
    del principal
    settings = cast(Settings, request.app.state.settings)
    provider = (
        "DeepSeek-compatible"
        if "deepseek" in settings.ai_base_url.lower()
        else "OpenAI-compatible"
    )
    return SafeSystemConfig(
        version=__version__,
        event_mode=settings.event_mode.value,
        timezone=settings.timezone,
        currency=settings.currency,
        worker_enabled=settings.worker_enabled,
        reply_worker_enabled=settings.reply_worker_enabled,
        cleanup_worker_enabled=settings.cleanup_enabled,
        pending_enabled=settings.pending_enabled,
        ai_provider=provider,
        ai_model=settings.ai_model,
        ai_api_key_configured=bool(settings.ai_api_key.strip()),
        lark_app_secret_configured=bool(settings.lark_app_secret.strip()),
        dashboard_base_url=settings.dashboard_base_url,
        session_ttl_seconds=settings.dashboard_session_ttl_seconds,
        secure_cookie=settings.dashboard_cookie_secure,
    )


def _analytics_dates(
    settings: Settings,
    period: AnalyticsPeriod,
    start_date: date | None,
    end_date: date | None,
) -> tuple[date, date]:
    today = datetime.now(ZoneInfo(settings.timezone)).date()
    if period == "custom":
        if start_date is None or end_date is None:
            raise HTTPException(status_code=422, detail="自定义范围需要开始和结束日期")
        result = (start_date, end_date)
    elif period == "year":
        result = (today.replace(month=1, day=1), today)
    else:
        days = {"7d": 7, "30d": 30, "90d": 90}[period]
        result = (today - timedelta(days=days - 1), today)
    try:
        local_date_bounds(result[0], result[1], ZoneInfo(settings.timezone))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return result


async def _analytics_data(
    request: Request,
    principal: DashboardPrincipal,
    period: AnalyticsPeriod,
    start_date: date | None,
    end_date: date | None,
) -> tuple[
    AnalyticsSummary,
    list[AnalyticsTrendPoint],
    list[AnalyticsCategory],
    list[AnalyticsMonthlyPoint],
]:
    settings = cast(Settings, request.app.state.settings)
    start, end = _analytics_dates(settings, period, start_date, end_date)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        return await WebAnalyticsQueryService(
            session, timezone=settings.timezone, currency=settings.currency
        ).analytics(principal.request_context, start_date=start, end_date=end)


@router.get("/analytics/summary", response_model=AnalyticsSummary)
async def analytics_summary(
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(current_principal)],
    period: AnalyticsPeriod = "30d",
    start_date: date | None = None,
    end_date: date | None = None,
) -> AnalyticsSummary:
    return (await _analytics_data(request, principal, period, start_date, end_date))[0]


@router.get("/analytics", response_model=AnalyticsOverview)
async def analytics_overview(
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(current_principal)],
    period: AnalyticsPeriod = "30d",
    start_date: date | None = None,
    end_date: date | None = None,
) -> AnalyticsOverview:
    summary, trend, categories, _ = await _analytics_data(
        request, principal, period, start_date, end_date
    )
    return AnalyticsOverview(summary=summary, trend=trend, categories=categories)


@router.get("/analytics/trend", response_model=list[AnalyticsTrendPoint])
async def analytics_trend(
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(current_principal)],
    period: AnalyticsPeriod = "30d",
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[AnalyticsTrendPoint]:
    return (await _analytics_data(request, principal, period, start_date, end_date))[1]


@router.get("/analytics/categories", response_model=list[AnalyticsCategory])
async def analytics_categories(
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(current_principal)],
    period: AnalyticsPeriod = "30d",
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[AnalyticsCategory]:
    return (await _analytics_data(request, principal, period, start_date, end_date))[2]


@router.get("/analytics/monthly", response_model=list[AnalyticsMonthlyPoint])
async def analytics_monthly(
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(current_principal)],
    period: AnalyticsPeriod = "year",
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[AnalyticsMonthlyPoint]:
    return (await _analytics_data(request, principal, period, start_date, end_date))[3]


async def _budget_overview(
    request: Request, principal: DashboardPrincipal
) -> BudgetOverview:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        return await WebAnalyticsQueryService(
            session, timezone=settings.timezone, currency=settings.currency
        ).budgets(principal.request_context)


@router.get("/budgets", response_model=BudgetOverview)
async def budgets(
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(current_principal)],
) -> BudgetOverview:
    return await _budget_overview(request, principal)


@router.put("/budgets/{category}", response_model=BudgetOverview)
async def update_budget(
    category: str,
    payload: BudgetUpdateRequest,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
) -> BudgetOverview:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    processor = getattr(request.app.state, "processor", None)
    async with factory() as session:
        await LedgerService(
            session,
            currency=settings.currency,
            timezone=settings.timezone,
            exchange_rates=getattr(processor, "exchange_rates", None),
        ).execute(
            principal.request_context,
            ParsedCommand(
                action=Action.SET_BUDGET,
                category=category,
                amount=payload.amount,
                currency=payload.currency,
            ),
        )
    return await _budget_overview(request, principal)


@router.delete("/budgets/{category}", response_model=BudgetOverview)
async def delete_budget(
    category: str,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
) -> BudgetOverview:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        await LedgerService(
            session, currency=settings.currency, timezone=settings.timezone
        ).execute(
            principal.request_context,
            ParsedCommand(action=Action.DELETE_BUDGET, category=category),
        )
    return await _budget_overview(request, principal)


@router.get("/reports", response_model=ReportData)
async def report(
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(current_principal)],
    start_date: date,
    end_date: date,
) -> ReportData:
    settings = cast(Settings, request.app.state.settings)
    try:
        start, end = local_date_bounds(start_date, end_date, ZoneInfo(settings.timezone))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        result = await LedgerService(
            session, currency=settings.currency, timezone=settings.timezone
        ).execute(
            principal.request_context,
            ParsedCommand(action=Action.REPORT, range_start=start, range_end=end),
        )
    if result.report is None:
        raise HTTPException(status_code=404, detail=result.message)
    return result.report


@router.post("/exports")
async def export_entries(
    payload: ExportRequestBody,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
) -> Response:
    settings = cast(Settings, request.app.state.settings)
    timezone = ZoneInfo(settings.timezone)
    today = datetime.now(timezone).date()
    range_start: datetime | None = None
    range_end: datetime | None = None
    export_start_date: date | None = None
    export_end_date: date | None = None
    export_all = payload.preset == "all"
    if payload.preset == "this_month":
        export_start_date, export_end_date = today.replace(day=1), today
    elif payload.preset == "custom":
        assert payload.start_date is not None and payload.end_date is not None
        export_start_date, export_end_date = payload.start_date, payload.end_date
    elif payload.preset == "last_90_days":
        export_start_date, export_end_date = today - timedelta(days=89), today
    if export_start_date is not None and export_end_date is not None:
        range_start = datetime.combine(
            export_start_date, time.min, tzinfo=timezone
        ).astimezone(UTC)
        range_end = datetime.combine(
            export_end_date + timedelta(days=1), time.min, tzinfo=timezone
        ).astimezone(UTC)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        result = await LedgerService(
            session, currency=settings.currency, timezone=settings.timezone
        ).execute(
            principal.request_context,
            ParsedCommand(
                action=Action.EXPORT_ENTRIES,
                range_start=range_start,
                range_end=range_end,
                export_all=export_all,
                include_deleted=payload.include_deleted,
            ),
        )
    if result.export is None:
        raise HTTPException(status_code=422, detail=result.message)
    return Response(
        content=result.export.content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{result.export.filename}"',
            "X-LarkLedger-Row-Count": str(result.export.row_count),
        },
    )
