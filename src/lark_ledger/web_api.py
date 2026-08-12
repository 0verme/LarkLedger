"""Authenticated API boundary for the optional Web Dashboard."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Annotated, Any, cast
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lark_ledger import __version__
from lark_ledger.client_schemas import (
    AccountVisibilityRequest,
    ClientAccount,
    ClientAccountBalance,
    ClientAccountCreateRequest,
    ClientAccountList,
    ClientAccountRenameRequest,
    ClientAssetSummary,
    ClientCredentialCreated,
    ClientCredentialCreateRequest,
    ClientCredentialList,
    ClientTransfer,
    ClientTransferCreateRequest,
)
from lark_ledger.config import Settings
from lark_ledger.confirmation_id import ConfirmationCodeError, normalize_confirmation_code
from lark_ledger.models import (
    Account,
    AccountVisibility,
    ClientIdempotencyRecord,
    Direction,
    FinancialGoal,
    Household,
    HouseholdInvitation,
    HouseholdMember,
    Ledger,
    LedgerEntry,
    Transfer,
    TransferRevision,
    User,
)
from lark_ledger.readiness import ReadinessService
from lark_ledger.schemas import Action, ParsedCommand, ReportData
from lark_ledger.services.accounts import AccountConflictError, AccountError, AccountNotFoundError
from lark_ledger.services.budget import parse_period
from lark_ledger.services.client_application import ClientApplicationService
from lark_ledger.services.client_auth import ClientCredentialService
from lark_ledger.services.client_idempotency import (
    ClientIdempotencyService,
    IdempotencyConflictError,
    IdempotencyInProgressError,
)
from lark_ledger.services.dashboard_auth import (
    CSRF_HEADER,
    OAUTH_COOKIE,
    DashboardAuthError,
    DashboardAuthService,
    DashboardPrincipal,
)
from lark_ledger.services.event_replay import EventReplayService
from lark_ledger.services.goals import (
    GoalConflictError,
    GoalError,
    GoalNotFoundError,
    GoalProgressService,
    GoalService,
)
from lark_ledger.services.household_management import (
    HouseholdConflictError,
    HouseholdManagementError,
    HouseholdManagementService,
    HouseholdNotFoundError,
    HouseholdPermissionError,
    HouseholdView,
)
from lark_ledger.services.insight_explanation import InsightExplanationService
from lark_ledger.services.ledger import EntryConflictError
from lark_ledger.services.ledger_authorization import LedgerAuthorizationError
from lark_ledger.services.ledger_management import (
    LedgerManagementError,
    LedgerManagementService,
    LedgerNameConflictError,
    LedgerNotFoundError,
)
from lark_ledger.services.pending import PendingCommandStore
from lark_ledger.services.recurring import (
    RecurringRuleConflictError,
    RecurringRuleError,
    RecurringRuleNotFoundError,
    RecurringRuleValidationError,
)
from lark_ledger.services.replay import OutboxReplayService
from lark_ledger.services.transfers import (
    AccountBalance,
    AssetSummary,
    TransferConflictError,
    TransferError,
    TransferNotFoundError,
)
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
    CurrentSession,
    DashboardData,
    DeletedFilter,
    EntryCreateRequest,
    EntryDetail,
    EntryPage,
    EntrySort,
    EntryUpdateRequest,
    EntryVersionRequest,
    EventReplayRequest,
    ExportRequestBody,
    GoalAccountBindingItem,
    GoalCreateRequest,
    GoalList,
    GoalProgress,
    GoalUpdateRequest,
    HouseholdCreateRequest,
    HouseholdInviteRequest,
    HouseholdList,
    HouseholdOverview,
    InsightList,
    LedgerList,
    LedgerNameRequest,
    MemberAliasRequest,
    MemberStats,
    PendingActionResponse,
    PendingDetail,
    PendingGroup,
    PendingPage,
    RecurringRuleCreateRequest,
    RecurringRuleList,
    RecurringRuleUpdateRequest,
    ResultReplayResponse,
    SafeSystemConfig,
    SessionList,
    SortOrder,
    TransferList,
    WebHousehold,
    WebHouseholdInvitation,
    WebHouseholdMember,
    WebLedger,
    WebRecurringRule,
    WebRevision,
    WebSession,
    WebTransferDetail,
)
from lark_ledger.web_schemas import (
    FinancialGoal as FinancialGoalView,
)

router = APIRouter(prefix="/api/web/v1", tags=["web-dashboard"])


def _auth_service(request: Request) -> DashboardAuthService:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    return DashboardAuthService(settings, factory)


def _auth_error(exc: DashboardAuthError, code: int = 401) -> HTTPException:
    return HTTPException(status_code=code, detail=str(exc))


def _client_ip(request: Request) -> str | None:
    """Best-effort client IP; only its SHA-256 digest is ever persisted."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    if request.client is not None:
        return request.client.host
    return None


def _require_same_origin(request: Request) -> None:
    """CSRF defence-in-depth: any Origin header on a state-changing request
    must match the dashboard origin (SameSite alone is not enough). Requests
    without an Origin header still must pass the double-submit CSRF check in
    ``csrf_principal``."""
    origin = request.headers.get("origin")
    if not origin:
        return
    settings = cast(Settings, request.app.state.settings)
    expected = urlsplit(settings.dashboard_base_url)
    actual = urlsplit(origin)
    if (actual.scheme, actual.netloc) != (expected.scheme, expected.netloc):
        raise DashboardAuthError("跨站请求被拒绝")


async def current_principal(
    request: Request,
    service: Annotated[DashboardAuthService, Depends(_auth_service)],
) -> DashboardPrincipal:
    session_token = request.cookies.get(service.session_cookie)
    try:
        return await service.authenticate(session_token)
    except DashboardAuthError as exc:
        raise _auth_error(exc) from exc


async def csrf_principal(
    request: Request,
    service: Annotated[DashboardAuthService, Depends(_auth_service)],
    csrf_header: Annotated[str | None, Header(alias=CSRF_HEADER)] = None,
) -> DashboardPrincipal:
    try:
        _require_same_origin(request)
        session_token = request.cookies.get(service.session_cookie)
        csrf_cookie = request.cookies.get(service.csrf_cookie)
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
        samesite=settings.dashboard_session_samesite,
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
        # A successful login ALWAYS creates a brand-new session (P37 §14): an
        # anonymous or pre-auth cookie is never upgraded in place.
        created = await service.create_session(
            identity,
            user_agent=request.headers.get("user-agent"),
            ip=_client_ip(request),
        )
    except DashboardAuthError as exc:
        raise _auth_error(exc) from exc
    settings = cast(Settings, request.app.state.settings)
    response = RedirectResponse(next_path, status_code=303)
    response.delete_cookie(
        OAUTH_COOKIE,
        path="/api/web/v1/auth/callback",
        secure=settings.dashboard_cookie_secure,
        httponly=True,
        samesite=settings.dashboard_session_samesite,
    )
    response.set_cookie(
        service.session_cookie,
        created.session_token,
        max_age=settings.dashboard_session_ttl_seconds,
        httponly=True,
        secure=settings.dashboard_cookie_secure,
        samesite=settings.dashboard_session_samesite,
        path="/",
    )
    response.set_cookie(
        service.csrf_cookie,
        created.csrf_token,
        max_age=settings.dashboard_session_ttl_seconds,
        httponly=False,
        secure=settings.dashboard_cookie_secure,
        samesite=settings.dashboard_session_samesite,
        path="/",
    )
    return response


@router.post("/auth/logout", status_code=204)
async def logout(
    request: Request,
    _: Annotated[DashboardPrincipal, Depends(csrf_principal)],
    service: Annotated[DashboardAuthService, Depends(_auth_service)],
) -> Response:
    # The session is revoked server-side BEFORE the cookie is cleared; deleting
    # the cookie alone never counts as logout (P37 §7).
    session_token = request.cookies.get(service.session_cookie)
    await service.revoke(session_token)
    settings = cast(Settings, request.app.state.settings)
    response = Response(status_code=204)
    response.delete_cookie(
        service.session_cookie,
        path="/",
        secure=settings.dashboard_cookie_secure,
        httponly=True,
        samesite=settings.dashboard_session_samesite,
    )
    response.delete_cookie(
        service.csrf_cookie,
        path="/",
        secure=settings.dashboard_cookie_secure,
        httponly=False,
        samesite=settings.dashboard_session_samesite,
    )
    return response


@router.get("/auth/session", response_model=CurrentSession)
async def current_session(
    principal: Annotated[DashboardPrincipal, Depends(current_principal)],
) -> CurrentSession:
    """Current session and identity (P37 §12). No credential material."""
    return CurrentSession(
        session_id=principal.session_id,
        open_id=principal.user_open_id,
        name=principal.display_name,
        avatar_url=principal.avatar_url,
        role=principal.role,
        expires_at=principal.expires_at,
    )


@router.get("/auth/sessions", response_model=SessionList)
async def list_sessions(
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(current_principal)],
) -> SessionList:
    service = _auth_service(request)
    sessions = await service.list_sessions(
        principal.user_id, current_session_id=principal.session_id
    )
    return SessionList(
        items=[_web_session(view) for view in sessions],
        current_session_id=principal.session_id,
    )


@router.delete("/auth/sessions/{session_id}", status_code=204)
async def revoke_session(
    request: Request,
    session_id: uuid.UUID,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
) -> Response:
    service = _auth_service(request)
    found = await service.revoke_session(principal.user_id, session_id)
    if not found:
        raise HTTPException(status_code=404, detail="会话不存在")
    return Response(status_code=204)


@router.post("/auth/sessions/revoke-others", status_code=204)
async def revoke_other_sessions(
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
) -> Response:
    service = _auth_service(request)
    await service.revoke_other_sessions(principal.user_id, principal.session_id)
    return Response(status_code=204)


def _web_session(view: Any) -> WebSession:
    return WebSession(
        id=view.session_id,
        created_at=view.created_at,
        last_seen_at=view.last_seen_at,
        expires_at=view.expires_at,
        revoked_at=view.revoked_at,
        current=view.current,
        device=view.device,
        user_agent=view.user_agent,
    )


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
        kind=ledger.kind,
        household_id=str(ledger.household_id) if ledger.household_id else None,
    )


@router.get("/client-credentials", response_model=ClientCredentialList)
async def list_client_credentials(
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(current_principal)],
) -> ClientCredentialList:
    async with cast(
        async_sessionmaker[AsyncSession], request.app.state.session_factory
    )() as session:
        items = await ClientCredentialService.list_for_user(session, principal.user_id)
    return ClientCredentialList(items=items)


@router.post("/client-credentials", response_model=ClientCredentialCreated, status_code=201)
async def create_client_credential(
    payload: ClientCredentialCreateRequest,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
) -> ClientCredentialCreated:
    async with cast(
        async_sessionmaker[AsyncSession], request.app.state.session_factory
    )() as session:
        try:
            return await ClientCredentialService.create(
                session,
                user_id=principal.user_id,
                current_ledger_id=principal.ledger_id,
                request=payload,
            )
        except ValueError as exc:
            await session.rollback()
            raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.delete("/client-credentials/{credential_id}", status_code=204)
async def revoke_client_credential(
    credential_id: uuid.UUID,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
) -> Response:
    async with cast(
        async_sessionmaker[AsyncSession], request.app.state.session_factory
    )() as session:
        try:
            await ClientCredentialService.revoke(
                session, user_id=principal.user_id, credential_id=credential_id
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="credential not found") from exc
    return Response(status_code=204)


def _ledger_http_error(exc: LedgerManagementError) -> HTTPException:
    if isinstance(exc, LedgerNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, LedgerNameConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


def _web_account(row: Account) -> ClientAccount:
    return ClientAccount.model_validate(
        {
            "id": str(row.id),
            "ledger_id": str(row.ledger_id),
            "name": row.name,
            "type": row.type,
            "subtype": row.subtype,
            "provider": row.provider,
            "currency": row.currency,
            "opening_balance": row.opening_balance,
            "status": row.status,
            "is_default": row.is_default,
            "visibility": row.visibility,
            "owner_user_id": str(row.owner_user_id) if row.owner_user_id else None,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
    )


def _web_transfer(row: Transfer) -> ClientTransfer:
    return ClientTransfer(
        id=str(row.id),
        ledger_id=str(row.ledger_id),
        from_account_id=str(row.from_account_id),
        to_account_id=str(row.to_account_id),
        amount=row.amount,
        currency=row.currency,
        note=row.note,
        occurred_at=row.occurred_at,
        reversed_at=row.reversed_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _web_balance(row: AccountBalance) -> ClientAccountBalance:
    return ClientAccountBalance(
        account_id=str(row.account_id),
        ledger_id=str(row.ledger_id),
        account_name=row.account_name,
        account_type=row.account_type,
        currency=row.currency,
        opening_balance=row.opening_balance,
        current_balance=row.current_balance,
        archived=row.archived,
    )


def _web_assets(row: AssetSummary) -> ClientAssetSummary:
    return ClientAssetSummary(
        ledger_id=str(row.ledger_id),
        currency=row.currency,
        total_assets=row.total_assets,
        total_liabilities=row.total_liabilities,
        net_assets=row.net_assets,
        accounts=[_web_balance(item) for item in row.accounts],
    )


def _account_http_error(exc: AccountError) -> HTTPException:
    if isinstance(exc, AccountNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, AccountConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


def _transfer_http_error(exc: TransferError) -> HTTPException:
    if isinstance(exc, TransferNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, TransferConflictError):
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
        ).list_accessible(principal.user_id)
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
            ).get_accessible(principal.user_id, principal.ledger_id)
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


@router.get("/accounts", response_model=ClientAccountList)
async def list_accounts(
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(current_principal)],
    include_archived: bool = False,
) -> ClientAccountList:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        rows = await ClientApplicationService(
            session, currency=settings.currency, timezone=settings.timezone
        ).list_accounts(principal.request_context, include_archived=include_archived)
    return ClientAccountList(items=[_web_account(row) for row in rows])


@router.get("/accounts/{account_id}", response_model=ClientAccount)
async def get_account(
    account_id: uuid.UUID,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(current_principal)],
) -> ClientAccount:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        try:
            row = await ClientApplicationService(
                session, currency=settings.currency, timezone=settings.timezone
            ).get_account(principal.request_context, account_id)
        except AccountError as exc:
            raise _account_http_error(exc) from exc
    return _web_account(row)


@router.post("/accounts", response_model=ClientAccount, status_code=201)
async def create_account(
    body: ClientAccountCreateRequest,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
) -> ClientAccount:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        try:
            row = await ClientApplicationService(
                session, currency=settings.currency, timezone=settings.timezone
            ).create_account(
                principal.request_context,
                name=body.name,
                account_type=body.type,
                subtype=body.subtype,
                provider=body.provider,
                currency=body.currency,
                opening_balance=body.opening_balance,
                make_default=body.is_default,
                visibility=AccountVisibility(body.visibility),
            )
            await session.commit()
        except AccountError as exc:
            await session.rollback()
            raise _account_http_error(exc) from exc
    return _web_account(row)


async def _mutate_web_account(
    account_id: uuid.UUID,
    request: Request,
    principal: DashboardPrincipal,
    *,
    operation: str,
    name: str | None = None,
) -> ClientAccount:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        app = ClientApplicationService(
            session, currency=settings.currency, timezone=settings.timezone
        )
        try:
            if operation == "rename":
                assert name is not None
                row = await app.rename_account(principal.request_context, account_id, name)
            elif operation == "archive":
                row = await app.archive_account(principal.request_context, account_id)
            else:
                row = await app.set_default_account(principal.request_context, account_id)
            await session.commit()
        except AccountError as exc:
            await session.rollback()
            raise _account_http_error(exc) from exc
    return _web_account(row)


@router.patch("/accounts/{account_id}", response_model=ClientAccount)
async def rename_account(
    account_id: uuid.UUID,
    body: ClientAccountRenameRequest,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
) -> ClientAccount:
    return await _mutate_web_account(
        account_id, request, principal, operation="rename", name=body.name
    )


@router.post("/accounts/{account_id}/archive", response_model=ClientAccount)
async def archive_account(
    account_id: uuid.UUID,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
) -> ClientAccount:
    return await _mutate_web_account(account_id, request, principal, operation="archive")


@router.post("/accounts/{account_id}/default", response_model=ClientAccount)
async def set_default_account(
    account_id: uuid.UUID,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
) -> ClientAccount:
    return await _mutate_web_account(account_id, request, principal, operation="default")


@router.post("/accounts/{account_id}/visibility", response_model=ClientAccount)
async def set_account_visibility(
    account_id: uuid.UUID,
    body: AccountVisibilityRequest,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
) -> ClientAccount:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        try:
            row = await ClientApplicationService(
                session, currency=settings.currency, timezone=settings.timezone
            ).set_account_visibility(
                principal.request_context,
                account_id,
                AccountVisibility(body.visibility),
            )
            await session.commit()
        except AccountError as exc:
            await session.rollback()
            raise _account_http_error(exc) from exc
    return _web_account(row)


@router.get("/accounts/{account_id}/balance", response_model=ClientAccountBalance)
async def get_account_balance(
    account_id: uuid.UUID,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(current_principal)],
) -> ClientAccountBalance:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        try:
            row = await ClientApplicationService(
                session, currency=settings.currency, timezone=settings.timezone
            ).account_balance(principal.request_context, account_id)
        except AccountError as exc:
            raise _account_http_error(exc) from exc
    return _web_balance(row)


@router.get("/assets", response_model=ClientAssetSummary)
async def get_assets(
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(current_principal)],
) -> ClientAssetSummary:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        row = await ClientApplicationService(
            session, currency=settings.currency, timezone=settings.timezone
        ).asset_summary(principal.request_context)
    return _web_assets(row)


@router.post("/transfers", response_model=ClientTransfer, status_code=201)
async def create_web_transfer(
    body: ClientTransferCreateRequest,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
) -> ClientTransfer:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        try:
            row = await ClientApplicationService(
                session, currency=settings.currency, timezone=settings.timezone
            ).create_transfer(
                principal.request_context,
                from_account_id=uuid.UUID(body.from_account_id),
                to_account_id=uuid.UUID(body.to_account_id),
                amount=body.amount,
                occurred_at=body.occurred_at,
                note=body.note,
                source_type="web",
            )
            await session.commit()
        except (ValueError, TransferError, AccountError) as exc:
            await session.rollback()
            if isinstance(exc, TransferError):
                raise _transfer_http_error(exc) from exc
            if isinstance(exc, AccountError):
                raise _account_http_error(exc) from exc
            raise HTTPException(status_code=422, detail="invalid account id") from exc
    return _web_transfer(row)


@router.get("/transfers", response_model=TransferList)
async def list_web_transfers(
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(current_principal)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> TransferList:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        rows, total = await ClientApplicationService(
            session, currency=settings.currency, timezone=settings.timezone
        ).list_transfers(principal.request_context, page=page, page_size=page_size)
    return TransferList(
        items=[_web_transfer(row) for row in rows],
        page=page,
        page_size=page_size,
        total=total,
        pages=(total + page_size - 1) // page_size if total else 0,
    )


def _web_revision(row: TransferRevision) -> WebRevision:
    return WebRevision(
        id=str(row.id),
        change_type=row.change_type,
        before=row.before_json,
        after=row.after_json,
        created_at=row.created_at,
    )


@router.get("/transfers/{transfer_id}", response_model=WebTransferDetail)
async def get_web_transfer(
    transfer_id: uuid.UUID,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(current_principal)],
) -> WebTransferDetail:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        app = ClientApplicationService(
            session, currency=settings.currency, timezone=settings.timezone
        )
        try:
            row = await app.get_transfer(principal.request_context, transfer_id)
            revisions = await app.transfer_revisions(principal.request_context, transfer_id)
        except TransferError as exc:
            raise _transfer_http_error(exc) from exc
    return WebTransferDetail(
        transfer=_web_transfer(row),
        revisions=[_web_revision(item) for item in revisions],
    )


@router.post("/transfers/{transfer_id}/reverse", response_model=ClientTransfer)
async def reverse_web_transfer(
    transfer_id: uuid.UUID,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
) -> ClientTransfer:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        try:
            row = await ClientApplicationService(
                session, currency=settings.currency, timezone=settings.timezone
            ).reverse_transfer(principal.request_context, transfer_id)
            await session.commit()
        except TransferError as exc:
            await session.rollback()
            raise _transfer_http_error(exc) from exc
    return _web_transfer(row)


def _household_http_error(exc: HouseholdManagementError) -> HTTPException:
    if isinstance(exc, HouseholdPermissionError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, HouseholdNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, HouseholdConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


async def _web_household(
    manager: HouseholdManagementService,
    view: HouseholdView,
    current_ledger_id: uuid.UUID,
    *,
    include_members: bool,
) -> WebHousehold:
    members = None
    if include_members:
        rows = await manager.list_members(view.membership.user_id, view.household.id)
        members = [
            WebHouseholdMember(
                user_id=str(item.user.id),
                display_name=item.user.display_name,
                role=item.membership.role,
                joined_at=item.membership.joined_at,
                alias=item.membership.alias,
            )
            for item in rows
        ]
    return WebHousehold(
        id=str(view.household.id),
        name=view.household.name,
        owner_user_id=str(view.household.owner_user_id),
        role=view.membership.role,
        status=view.household.status,
        ledger=_web_ledger(view.ledger, current_ledger_id),
        created_at=view.household.created_at,
        updated_at=view.household.updated_at,
        members=members,
    )


@router.get("/households", response_model=HouseholdList)
async def list_households(
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(current_principal)],
) -> HouseholdList:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        manager = HouseholdManagementService(
            session, currency=settings.currency, timezone=settings.timezone
        )
        views = await manager.list_for_user(principal.user_id)
        return HouseholdList(
            items=[
                await _web_household(manager, view, principal.ledger_id, include_members=False)
                for view in views
            ]
        )


@router.post("/households", response_model=WebHousehold, status_code=201)
async def create_household(
    body: HouseholdCreateRequest,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
) -> WebHousehold:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        manager = HouseholdManagementService(
            session, currency=settings.currency, timezone=settings.timezone
        )
        try:
            view = await manager.create(principal.user_id, body.name)
            await session.commit()
        except HouseholdManagementError as exc:
            await session.rollback()
            raise _household_http_error(exc) from exc
        except IntegrityError as exc:
            await session.rollback()
            raise HTTPException(status_code=409, detail="家庭名称或公共账本冲突") from exc
        return await _web_household(manager, view, principal.ledger_id, include_members=True)


@router.get("/households/{household_id}", response_model=WebHousehold)
async def household_detail(
    household_id: uuid.UUID,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(current_principal)],
) -> WebHousehold:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        manager = HouseholdManagementService(
            session, currency=settings.currency, timezone=settings.timezone
        )
        try:
            view = await manager.get(principal.user_id, household_id)
        except HouseholdManagementError as exc:
            raise _household_http_error(exc) from exc
        return await _web_household(manager, view, principal.ledger_id, include_members=True)


@router.patch("/households/{household_id}", response_model=WebHousehold)
async def rename_household(
    household_id: uuid.UUID,
    body: HouseholdCreateRequest,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
) -> WebHousehold:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        manager = HouseholdManagementService(
            session, currency=settings.currency, timezone=settings.timezone
        )
        try:
            view = await manager.rename(principal.user_id, household_id, body.name)
            await session.commit()
        except HouseholdManagementError as exc:
            await session.rollback()
            raise _household_http_error(exc) from exc
        return await _web_household(manager, view, principal.ledger_id, include_members=True)


@router.get("/households/{household_id}/members", response_model=list[WebHouseholdMember])
async def household_members(
    household_id: uuid.UUID,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(current_principal)],
) -> list[WebHouseholdMember]:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        manager = HouseholdManagementService(
            session, currency=settings.currency, timezone=settings.timezone
        )
        try:
            rows = await manager.list_members(principal.user_id, household_id)
        except HouseholdManagementError as exc:
            raise _household_http_error(exc) from exc
        return [
            WebHouseholdMember(
                user_id=str(item.user.id),
                display_name=item.user.display_name,
                role=item.membership.role,
                joined_at=item.membership.joined_at,
                alias=item.membership.alias,
            )
            for item in rows
        ]


@router.patch("/households/{household_id}/members/{user_id}", response_model=WebHouseholdMember)
async def update_household_member_alias(
    household_id: uuid.UUID,
    user_id: uuid.UUID,
    body: MemberAliasRequest,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
) -> WebHouseholdMember:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        manager = HouseholdManagementService(
            session, currency=settings.currency, timezone=settings.timezone
        )
        try:
            membership = await manager.set_member_alias(
                principal.user_id, household_id, user_id, body.alias
            )
            await session.commit()
        except HouseholdManagementError as exc:
            await session.rollback()
            raise _household_http_error(exc) from exc
        row = await session.get(HouseholdMember, membership.id)
        member_user = await session.get(User, user_id)
    return WebHouseholdMember(
        user_id=str(user_id),
        display_name=member_user.display_name if member_user else "",
        role=row.role if row else "",
        joined_at=row.joined_at if row else None,
        alias=row.alias if row else None,
    )


@router.get("/ledgers/{ledger_id}/members/stats", response_model=list[MemberStats])
async def member_stats(
    ledger_id: uuid.UUID,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(current_principal)],
) -> list[MemberStats]:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        try:
            return await ClientApplicationService(
                session, currency=settings.currency, timezone=settings.timezone
            ).member_stats(principal.request_context, ledger_id)
        except LedgerAuthorizationError as exc:
            raise HTTPException(status_code=404, detail="账本不存在或当前用户无权访问") from exc


async def _web_invitation(
    session: AsyncSession, invitation: HouseholdInvitation
) -> WebHouseholdInvitation:
    household = await session.get(Household, invitation.household_id)
    return WebHouseholdInvitation(
        id=str(invitation.id),
        invitation_code=invitation.public_id,
        household_id=str(invitation.household_id),
        household_name=household.name if household else "未知家庭",
        target_user_id=str(invitation.target_user_id),
        status=invitation.status,
        expires_at=invitation.expires_at,
        created_at=invitation.created_at,
    )


@router.post(
    "/households/{household_id}/invitations",
    response_model=WebHouseholdInvitation,
    status_code=201,
)
async def invite_household_member(
    household_id: uuid.UUID,
    body: HouseholdInviteRequest,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
) -> WebHouseholdInvitation:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        manager = HouseholdManagementService(
            session, currency=settings.currency, timezone=settings.timezone
        )
        try:
            invitation = await manager.invite(principal.user_id, household_id, body.target)
            await session.commit()
        except HouseholdManagementError as exc:
            await session.rollback()
            raise _household_http_error(exc) from exc
        except IntegrityError as exc:
            await session.rollback()
            raise HTTPException(status_code=409, detail="该用户已有待处理邀请") from exc
        return await _web_invitation(session, invitation)


@router.get("/household-invitations", response_model=list[WebHouseholdInvitation])
async def household_invitations(
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(current_principal)],
) -> list[WebHouseholdInvitation]:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        manager = HouseholdManagementService(
            session, currency=settings.currency, timezone=settings.timezone
        )
        rows = await manager.list_invitations(principal.user_id)
        await session.commit()
        return [await _web_invitation(session, item) for item in rows]


async def _respond_invitation(
    *,
    invitation_id: uuid.UUID,
    request: Request,
    principal: DashboardPrincipal,
    action: str,
) -> WebHouseholdInvitation:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        manager = HouseholdManagementService(
            session, currency=settings.currency, timezone=settings.timezone
        )
        try:
            if action == "accept":
                invitation = await manager.accept(principal.user_id, invitation_id)
            elif action == "reject":
                invitation = await manager.reject(principal.user_id, invitation_id)
            else:
                invitation = await manager.cancel_invitation(principal.user_id, invitation_id)
            await session.commit()
        except HouseholdManagementError as exc:
            await session.rollback()
            raise _household_http_error(exc) from exc
        return await _web_invitation(session, invitation)


@router.post(
    "/household-invitations/{invitation_id}/accept",
    response_model=WebHouseholdInvitation,
)
async def accept_household_invitation(
    invitation_id: uuid.UUID,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
) -> WebHouseholdInvitation:
    return await _respond_invitation(
        invitation_id=invitation_id, request=request, principal=principal, action="accept"
    )


@router.post(
    "/household-invitations/{invitation_id}/reject",
    response_model=WebHouseholdInvitation,
)
async def reject_household_invitation(
    invitation_id: uuid.UUID,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
) -> WebHouseholdInvitation:
    return await _respond_invitation(
        invitation_id=invitation_id, request=request, principal=principal, action="reject"
    )


@router.post(
    "/household-invitations/{invitation_id}/cancel",
    response_model=WebHouseholdInvitation,
)
async def cancel_household_invitation(
    invitation_id: uuid.UUID,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
) -> WebHouseholdInvitation:
    return await _respond_invitation(
        invitation_id=invitation_id, request=request, principal=principal, action="cancel"
    )


@router.post("/households/{household_id}/leave", status_code=204)
async def leave_household(
    household_id: uuid.UUID,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
) -> Response:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        manager = HouseholdManagementService(
            session, currency=settings.currency, timezone=settings.timezone
        )
        try:
            await manager.leave(principal.user_id, household_id)
            await session.commit()
        except HouseholdManagementError as exc:
            await session.rollback()
            raise _household_http_error(exc) from exc
    return Response(status_code=204)


@router.delete("/households/{household_id}/members/{user_id}", status_code=204)
async def remove_household_member(
    household_id: uuid.UUID,
    user_id: uuid.UUID,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
) -> Response:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        manager = HouseholdManagementService(
            session, currency=settings.currency, timezone=settings.timezone
        )
        try:
            await manager.remove_member(principal.user_id, household_id, user_id)
            await session.commit()
        except HouseholdManagementError as exc:
            await session.rollback()
            raise _household_http_error(exc) from exc
    return Response(status_code=204)


@router.get("/dashboard", response_model=DashboardData)
async def dashboard(
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(current_principal)],
) -> DashboardData:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        return await WebLedgerQueryService(
            session, timezone=settings.timezone, currency=settings.currency
        ).dashboard(principal.request_context)


@router.get("/overview", response_model=HouseholdOverview)
async def overview(
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(current_principal)],
    period: str | None = None,
) -> HouseholdOverview:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    target = _budget_period(period)
    async with factory() as session:
        return await ClientApplicationService(
            session, currency=settings.currency, timezone=settings.timezone
        ).household_overview(principal.request_context, period=target)


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
        return await WebLedgerQueryService(session, timezone=settings.timezone).list_entries(
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


@router.post(
    "/entries",
    response_model=EntryDetail,
    status_code=201,
    responses={
        409: {"description": "Idempotency-Key was already used with a different request"},
        503: {"description": "The idempotent request is still in progress"},
    },
)
async def create_web_entry(
    body: EntryCreateRequest,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> EntryDetail:
    """Create one ledger entry from the First-party Web client.

    Idempotency contract (mirrors the machine ``/api/v1`` family): a browser
    retry after a timeout, double-click or React double-fire replays the same
    ``Idempotency-Key`` and returns the stored response instead of creating a
    second ledger row — the ledger entry is created exactly once (P38 §13).
    """
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    source_id = f"web:{uuid.uuid4()}"
    async with factory() as session:
        app = ClientApplicationService(
            session, currency=settings.currency, timezone=settings.timezone
        )

        async def apply(_record: ClientIdempotencyRecord) -> dict[str, Any]:
            await app.execute_financial(
                principal.request_context,
                ParsedCommand(
                    action=Action.CREATE,
                    amount=body.amount,
                    direction=body.direction,
                    category=body.category,
                    note=body.note,
                    occurred_at=body.occurred_at,
                    currency=body.currency,
                ),
                source_type="web",
                source_message_id=source_id,
                # The idempotency service owns the commit so the entry and its
                # idempotency record land in the same transaction (P38 §13).
                commit_changes=False,
                account_id=body.account_id,
                paid_by_user_id=body.paid_by_user_id,
            )
            entry = await session.scalar(
                select(LedgerEntry).where(
                    LedgerEntry.ledger_id == principal.request_context.ledger_id,
                    LedgerEntry.source_message_id == source_id,
                )
            )
            if entry is None:
                raise ValueError("账目保存后未能读取，请稍后重试")
            detail = await WebLedgerQueryService(session, timezone=settings.timezone).entry_detail(
                principal.request_context, entry.short_id
            )
            if detail is None:
                raise ValueError("账目保存后未能读取，请稍后重试")
            return detail.model_dump(mode="json")

        try:
            data, _replayed = await ClientIdempotencyService(session).execute(
                principal.request_context,
                operation="web.entry.create",
                key=idempotency_key or "",
                payload={
                    "amount": str(body.amount),
                    "direction": body.direction,
                    "category": body.category,
                    "note": body.note,
                    "occurred_at": body.occurred_at.isoformat(),
                    "currency": body.currency,
                    "account_id": str(body.account_id) if body.account_id else None,
                    "paid_by_user_id": (
                        str(body.paid_by_user_id) if body.paid_by_user_id else None
                    ),
                },
                callback=apply,
                response_status=201,
            )
        except IdempotencyConflictError as exc:
            await session.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except IdempotencyInProgressError as exc:
            await session.rollback()
            raise HTTPException(status_code=503, detail="账目正在保存中，请稍后重试") from exc
        except AccountError as exc:
            await session.rollback()
            raise _account_http_error(exc) from exc
        except ValueError as exc:
            await session.rollback()
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return EntryDetail.model_validate(data)


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
            detail = await WebLedgerQueryService(session, timezone=settings.timezone).entry_detail(
                principal.request_context, short_id
            )
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
    account_id: uuid.UUID | None = None,
    paid_by_user_id: uuid.UUID | None = None,
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
            await ClientApplicationService(
                session,
                currency=settings.currency,
                timezone=settings.timezone,
            ).execute_financial(
                principal.request_context,
                command,
                source_type="web",
                expected_updated_at=expected_updated_at,
                account_id=account_id,
                paid_by_user_id=paid_by_user_id,
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
        account_id=payload.account_id,
        paid_by_user_id=payload.paid_by_user_id,
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
        getattr(processor, "_pending_store", None) or PendingCommandStore(factory, settings),
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


@router.post("/admin/outbox/{outbox_id}/replay", response_model=ResultReplayResponse)
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
        "DeepSeek-compatible" if "deepseek" in settings.ai_base_url.lower() else "OpenAI-compatible"
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


def _budget_period(period: str | None) -> date | None:
    if period is None or not period.strip():
        return None
    try:
        return parse_period(period)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _budget_overview(
    request: Request, principal: DashboardPrincipal, period: date | None = None
) -> BudgetOverview:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        return await ClientApplicationService(
            session, currency=settings.currency, timezone=settings.timezone
        ).get_budget_overview(principal.request_context, period=period)


@router.get("/budgets", response_model=BudgetOverview)
async def budgets(
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(current_principal)],
    period: str | None = None,
) -> BudgetOverview:
    return await _budget_overview(request, principal, _budget_period(period))


@router.put("/budgets/total", response_model=BudgetOverview)
async def update_total_budget(
    payload: BudgetUpdateRequest,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
    period: str | None = None,
) -> BudgetOverview:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    target = _budget_period(period)
    async with factory() as session:
        await ClientApplicationService(
            session, currency=settings.currency, timezone=settings.timezone
        ).set_total_budget(
            principal.request_context,
            period=target,
            amount=payload.amount,
            currency=payload.currency,
        )
        await session.commit()
    return await _budget_overview(request, principal, target)


@router.delete("/budgets/total", response_model=BudgetOverview)
async def delete_total_budget(
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
    period: str | None = None,
) -> BudgetOverview:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    target = _budget_period(period)
    async with factory() as session:
        await ClientApplicationService(
            session, currency=settings.currency, timezone=settings.timezone
        ).delete_budget(principal.request_context, period=target, category=None)
        await session.commit()
    return await _budget_overview(request, principal, target)


@router.put("/budgets/{category}", response_model=BudgetOverview)
async def update_budget(
    category: str,
    payload: BudgetUpdateRequest,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
    period: str | None = None,
) -> BudgetOverview:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    target = _budget_period(period)
    async with factory() as session:
        try:
            await ClientApplicationService(
                session, currency=settings.currency, timezone=settings.timezone
            ).set_category_budget(
                principal.request_context,
                period=target,
                category=category,
                amount=payload.amount,
                currency=payload.currency,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        await session.commit()
    return await _budget_overview(request, principal, target)


@router.delete("/budgets/{category}", response_model=BudgetOverview)
async def delete_budget(
    category: str,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
    period: str | None = None,
) -> BudgetOverview:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    target = _budget_period(period)
    async with factory() as session:
        await ClientApplicationService(
            session, currency=settings.currency, timezone=settings.timezone
        ).delete_budget(principal.request_context, period=target, category=category)
        await session.commit()
    return await _budget_overview(request, principal, target)


def _recurring_http_error(exc: RecurringRuleError) -> HTTPException:
    if isinstance(exc, RecurringRuleNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, RecurringRuleConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, RecurringRuleValidationError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


@router.get("/recurring-rules", response_model=RecurringRuleList)
async def list_recurring_rules(
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(current_principal)],
) -> RecurringRuleList:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        views = await ClientApplicationService(
            session, currency=settings.currency, timezone=settings.timezone
        ).recurring_rule_views(principal.request_context)
    return RecurringRuleList(items=views)


@router.post("/recurring-rules", response_model=WebRecurringRule, status_code=201)
async def create_recurring_rule(
    body: RecurringRuleCreateRequest,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
) -> WebRecurringRule:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        try:
            row = await ClientApplicationService(
                session, currency=settings.currency, timezone=settings.timezone
            ).create_recurring_rule(
                principal.request_context,
                transaction_type=body.transaction_type,
                amount=body.amount,
                currency=body.currency,
                category=body.category,
                description=body.description,
                frequency=body.frequency,
                interval=body.interval,
                next_occurrence=body.next_occurrence,
                account_id=body.account_id,
                paid_by_user_id=body.paid_by_user_id,
            )
            await session.commit()
        except RecurringRuleError as exc:
            await session.rollback()
            raise _recurring_http_error(exc) from exc
        return await _recurring_rule_view(request, principal, row.id)


@router.get("/recurring-rules/{rule_id}", response_model=WebRecurringRule)
async def get_recurring_rule(
    rule_id: uuid.UUID,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(current_principal)],
) -> WebRecurringRule:
    return await _recurring_rule_view(request, principal, rule_id)


@router.patch("/recurring-rules/{rule_id}", response_model=WebRecurringRule)
async def update_recurring_rule(
    rule_id: uuid.UUID,
    body: RecurringRuleUpdateRequest,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
) -> WebRecurringRule:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        try:
            await ClientApplicationService(
                session, currency=settings.currency, timezone=settings.timezone
            ).update_recurring_rule(
                principal.request_context,
                rule_id,
                transaction_type=body.transaction_type,
                amount=body.amount,
                currency=body.currency,
                category=body.category,
                description=body.description,
                frequency=body.frequency,
                interval=body.interval,
                next_occurrence=body.next_occurrence,
                account_id=body.account_id,
                paid_by_user_id=body.paid_by_user_id,
            )
            await session.commit()
        except RecurringRuleError as exc:
            await session.rollback()
            raise _recurring_http_error(exc) from exc
        return await _recurring_rule_view(request, principal, rule_id)


@router.post("/recurring-rules/{rule_id}/pause", response_model=WebRecurringRule)
async def pause_recurring_rule(
    rule_id: uuid.UUID,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
) -> WebRecurringRule:
    await _recurring_mutate(request, principal, rule_id, "pause")
    return await _recurring_rule_view(request, principal, rule_id)


@router.post("/recurring-rules/{rule_id}/resume", response_model=WebRecurringRule)
async def resume_recurring_rule(
    rule_id: uuid.UUID,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
) -> WebRecurringRule:
    await _recurring_mutate(request, principal, rule_id, "resume")
    return await _recurring_rule_view(request, principal, rule_id)


@router.post("/recurring-rules/{rule_id}/disable", response_model=WebRecurringRule)
async def disable_recurring_rule(
    rule_id: uuid.UUID,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
) -> WebRecurringRule:
    await _recurring_mutate(request, principal, rule_id, "disable")
    return await _recurring_rule_view(request, principal, rule_id)


@router.post("/recurring-rules/{rule_id}/skip", response_model=WebRecurringRule)
async def skip_recurring_occurrence(
    rule_id: uuid.UUID,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
) -> WebRecurringRule:
    await _recurring_mutate(request, principal, rule_id, "skip")
    return await _recurring_rule_view(request, principal, rule_id)


async def _recurring_rule_view(
    request: Request, principal: DashboardPrincipal, rule_id: uuid.UUID
) -> WebRecurringRule:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        try:
            return await ClientApplicationService(
                session, currency=settings.currency, timezone=settings.timezone
            ).recurring_rule_view(principal.request_context, rule_id)
        except RecurringRuleError as exc:
            raise _recurring_http_error(exc) from exc


async def _recurring_mutate(
    request: Request,
    principal: DashboardPrincipal,
    rule_id: uuid.UUID,
    action: str,
) -> None:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        app = ClientApplicationService(
            session, currency=settings.currency, timezone=settings.timezone
        )
        try:
            if action == "pause":
                await app.pause_recurring_rule(principal.request_context, rule_id)
            elif action == "resume":
                await app.resume_recurring_rule(principal.request_context, rule_id)
            elif action == "disable":
                await app.disable_recurring_rule(principal.request_context, rule_id)
            else:
                await app.skip_recurring_occurrence(principal.request_context, rule_id)
            await session.commit()
        except RecurringRuleError as exc:
            await session.rollback()
            raise _recurring_http_error(exc) from exc


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
        result = await ClientApplicationService(
            session, currency=settings.currency, timezone=settings.timezone
        ).execute_financial(
            principal.request_context,
            ParsedCommand(action=Action.REPORT, range_start=start, range_end=end),
            source_type="web",
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
        range_start = datetime.combine(export_start_date, time.min, tzinfo=timezone).astimezone(UTC)
        range_end = datetime.combine(
            export_end_date + timedelta(days=1), time.min, tzinfo=timezone
        ).astimezone(UTC)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        result = await ClientApplicationService(
            session, currency=settings.currency, timezone=settings.timezone
        ).execute_financial(
            principal.request_context,
            ParsedCommand(
                action=Action.EXPORT_ENTRIES,
                range_start=range_start,
                range_end=range_end,
                export_all=export_all,
                include_deleted=payload.include_deleted,
            ),
            source_type="web",
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


# ---------------------------------------------------------------------------
# P33 — Financial goals & deterministic insights
# ---------------------------------------------------------------------------


def _goal_http_error(exc: GoalError) -> HTTPException:
    if isinstance(exc, GoalNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, GoalConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


def _web_goal(
    goal: FinancialGoal,
    bindings: list[GoalAccountBindingItem],
    progress: GoalProgress | None,
) -> FinancialGoalView:
    return FinancialGoalView(
        id=str(goal.id),
        ledger_id=str(goal.ledger_id),
        name=goal.name,
        description=goal.description,
        goal_type=goal.goal_type,
        target_amount=goal.target_amount,
        currency=goal.currency,
        target_date=goal.target_date,
        status=goal.status,
        created_by_user_id=str(goal.created_by_user_id),
        created_at=goal.created_at,
        updated_at=goal.updated_at,
        account_bindings=bindings,
        current_amount=progress.current_amount if progress else Decimal("0"),
        remaining_amount=progress.remaining_amount if progress else None,
        progress_percent=progress.progress_percent if progress else None,
        is_target_reached=progress.is_target_reached if progress else False,
    )


@router.get("/goals", response_model=GoalList)
async def list_goals(
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(current_principal)],
) -> GoalList:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        application = ClientApplicationService(
            session, currency=settings.currency, timezone=settings.timezone
        )
        pairs = await application.goal_list_with_progress(principal.request_context)
        service = GoalService(session, timezone=settings.timezone, currency=settings.currency)
        items = [
            _web_goal(
                goal,
                await service.binding_items(principal.request_context, goal.id),
                progress,
            )
            for goal, progress in pairs
        ]
        return GoalList(items=items)


@router.post("/goals", response_model=FinancialGoalView, status_code=201)
async def create_goal(
    payload: GoalCreateRequest,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
) -> FinancialGoalView:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        application = ClientApplicationService(
            session, currency=settings.currency, timezone=settings.timezone
        )
        try:
            goal = await application.create_goal(
                principal.request_context,
                name=payload.name,
                target_amount=payload.target_amount,
                currency=payload.currency,
                description=payload.description,
                target_date=payload.target_date,
                account_ids=payload.account_ids,
            )
            await session.commit()
            await session.refresh(goal)
        except GoalError as exc:
            await session.rollback()
            raise _goal_http_error(exc) from exc
        service = GoalService(session, timezone=settings.timezone, currency=settings.currency)
        bindings = await service.binding_items(principal.request_context, goal.id)
        progress = await GoalProgressService(
            session, timezone=settings.timezone, currency=settings.currency
        ).progress(principal.request_context, goal)
        return _web_goal(goal, bindings, progress)


@router.get("/goals/{goal_id}", response_model=FinancialGoalView)
async def get_goal(
    goal_id: uuid.UUID,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(current_principal)],
) -> FinancialGoalView:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        service = GoalService(session, timezone=settings.timezone, currency=settings.currency)
        try:
            goal = await service.get(principal.request_context, goal_id)
            bindings = await service.binding_items(principal.request_context, goal.id)
            progress = await GoalProgressService(
                session, timezone=settings.timezone, currency=settings.currency
            ).progress(principal.request_context, goal)
        except GoalError as exc:
            raise _goal_http_error(exc) from exc
        return _web_goal(goal, bindings, progress)


@router.patch("/goals/{goal_id}", response_model=FinancialGoalView)
async def update_goal(
    goal_id: uuid.UUID,
    payload: GoalUpdateRequest,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
) -> FinancialGoalView:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    updates: dict[str, Any] = payload.model_dump(exclude_unset=True)
    async with factory() as session:
        application = ClientApplicationService(
            session, currency=settings.currency, timezone=settings.timezone
        )
        try:
            goal = await application.update_goal(principal.request_context, goal_id, **updates)
            await session.commit()
            await session.refresh(goal)
        except GoalError as exc:
            await session.rollback()
            raise _goal_http_error(exc) from exc
        service = GoalService(session, timezone=settings.timezone, currency=settings.currency)
        bindings = await service.binding_items(principal.request_context, goal.id)
        progress = await GoalProgressService(
            session, timezone=settings.timezone, currency=settings.currency
        ).progress(principal.request_context, goal)
        return _web_goal(goal, bindings, progress)


@router.delete("/goals/{goal_id}", status_code=204)
async def delete_goal(
    goal_id: uuid.UUID,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
) -> Response:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        application = ClientApplicationService(
            session, currency=settings.currency, timezone=settings.timezone
        )
        try:
            await application.delete_goal(principal.request_context, goal_id)
            await session.commit()
        except GoalError as exc:
            await session.rollback()
            raise _goal_http_error(exc) from exc
    return Response(status_code=204)


@router.post("/goals/{goal_id}/complete", response_model=FinancialGoalView)
async def complete_goal(
    goal_id: uuid.UUID,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
) -> FinancialGoalView:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        application = ClientApplicationService(
            session, currency=settings.currency, timezone=settings.timezone
        )
        try:
            goal = await application.complete_goal(principal.request_context, goal_id)
            await session.commit()
            await session.refresh(goal)
        except GoalError as exc:
            await session.rollback()
            raise _goal_http_error(exc) from exc
        service = GoalService(session, timezone=settings.timezone, currency=settings.currency)
        bindings = await service.binding_items(principal.request_context, goal.id)
        progress = await GoalProgressService(
            session, timezone=settings.timezone, currency=settings.currency
        ).progress(principal.request_context, goal)
        return _web_goal(goal, bindings, progress)


@router.post("/goals/{goal_id}/archive", response_model=FinancialGoalView)
async def archive_goal(
    goal_id: uuid.UUID,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(csrf_principal)],
) -> FinancialGoalView:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        application = ClientApplicationService(
            session, currency=settings.currency, timezone=settings.timezone
        )
        try:
            goal = await application.archive_goal(principal.request_context, goal_id)
            await session.commit()
            await session.refresh(goal)
        except GoalError as exc:
            await session.rollback()
            raise _goal_http_error(exc) from exc
        service = GoalService(session, timezone=settings.timezone, currency=settings.currency)
        bindings = await service.binding_items(principal.request_context, goal.id)
        progress = await GoalProgressService(
            session, timezone=settings.timezone, currency=settings.currency
        ).progress(principal.request_context, goal)
        return _web_goal(goal, bindings, progress)


@router.get("/goals/{goal_id}/progress", response_model=GoalProgress)
async def goal_progress(
    goal_id: uuid.UUID,
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(current_principal)],
) -> GoalProgress:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    async with factory() as session:
        application = ClientApplicationService(
            session, currency=settings.currency, timezone=settings.timezone
        )
        try:
            return await application.goal_progress(principal.request_context, goal_id)
        except GoalError as exc:
            raise _goal_http_error(exc) from exc


@router.get("/insights", response_model=InsightList)
async def insights(
    request: Request,
    principal: Annotated[DashboardPrincipal, Depends(current_principal)],
    period: str | None = None,
    limit: Annotated[int | None, Query(ge=1, le=20)] = None,
    explain: bool = False,
) -> InsightList:
    settings = cast(Settings, request.app.state.settings)
    factory = cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)
    target = _budget_period(period)
    async with factory() as session:
        application = ClientApplicationService(
            session, currency=settings.currency, timezone=settings.timezone
        )
        items = await application.insights(principal.request_context, period=target, limit=limit)
        if explain:
            explainer = InsightExplanationService(settings)
            for item in items:
                item.explanation = await explainer.explain(item)
        return InsightList(insights=items)
