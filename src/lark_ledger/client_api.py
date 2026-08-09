from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lark_ledger.client_schemas import (
    ClientAccount,
    ClientAccountCreateRequest,
    ClientAccountList,
    ClientAccountRenameRequest,
    ClientCommandResult,
    ClientEntryCreateRequest,
    ClientErrorResponse,
    ClientIdentity,
    ClientLedger,
    ClientLedgerList,
    ClientLedgerNameRequest,
)
from lark_ledger.config import Settings
from lark_ledger.confirmation_id import ConfirmationCodeError, normalize_confirmation_code
from lark_ledger.models import (
    Account,
    ClientIdempotencyRecord,
    ClientSecurityAudit,
    Household,
    Ledger,
    LedgerEntry,
)
from lark_ledger.schemas import Action, ParsedCommand, ReportData
from lark_ledger.services.accounts import AccountConflictError, AccountError, AccountNotFoundError
from lark_ledger.services.client_application import ClientApplicationService, EntryQuery
from lark_ledger.services.client_auth import (
    ClientAuthenticationError,
    ClientCredentialService,
    ClientPrincipal,
    ClientScopeError,
)
from lark_ledger.services.client_idempotency import (
    ClientIdempotencyService,
    IdempotencyConflictError,
    IdempotencyInProgressError,
)
from lark_ledger.services.household_management import (
    HouseholdManagementError,
    HouseholdView,
)
from lark_ledger.services.ledger import EntryConflictError
from lark_ledger.services.ledger_authorization import LedgerAuthorizationError
from lark_ledger.services.ledger_management import LedgerManagementError
from lark_ledger.services.pending import PendingCommandStore
from lark_ledger.web_schemas import (
    AnalyticsOverview,
    BudgetOverview,
    BudgetUpdateRequest,
    DashboardData,
    DeletedFilter,
    EntryDetail,
    EntryPage,
    EntrySort,
    EntryUpdateRequest,
    EntryVersionRequest,
    HouseholdCreateRequest,
    HouseholdInviteRequest,
    HouseholdList,
    PendingActionResponse,
    PendingDetail,
    PendingGroup,
    PendingPage,
    SortOrder,
    WebHousehold,
    WebHouseholdInvitation,
    WebHouseholdMember,
    WebLedger,
)

router = APIRouter(prefix="/api/client/v1", tags=["client-v1"])
ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ClientErrorResponse},
    403: {"model": ClientErrorResponse},
    404: {"model": ClientErrorResponse},
    409: {"model": ClientErrorResponse},
    422: {"model": ClientErrorResponse},
    503: {"model": ClientErrorResponse},
}


def client_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def _factory(request: Request) -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], request.app.state.session_factory)


async def client_principal(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> ClientPrincipal:
    token = None
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value:
            token = value
    settings = _settings(request)
    try:
        return await ClientCredentialService(
            _factory(request), currency=settings.currency, timezone=settings.timezone
        ).authenticate(token)
    except ClientAuthenticationError as exc:
        code = "expired" if "expired" in str(exc) else "authentication_required"
        raise client_error(401, code, "valid bearer credential required") from exc


def _require(principal: ClientPrincipal, scope: str) -> None:
    try:
        principal.require(scope)
    except ClientScopeError as exc:
        raise client_error(403, "permission_denied", "credential scope denied") from exc


def _ledger(row: Ledger, current_id: uuid.UUID) -> ClientLedger:
    return ClientLedger(
        id=str(row.id),
        name=row.name,
        kind=row.kind,
        currency=row.currency,
        timezone=row.timezone,
        is_default=row.is_default,
        is_current=row.id == current_id,
        household_id=str(row.household_id) if row.household_id else None,
    )


def _account(row: Account) -> ClientAccount:
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
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
    )


def _household(view: HouseholdView, current_id: uuid.UUID) -> WebHousehold:
    return WebHousehold(
        id=str(view.household.id),
        name=view.household.name,
        owner_user_id=str(view.household.owner_user_id),
        role=view.membership.role,
        status=view.household.status,
        ledger=WebLedger.model_validate(_ledger(view.ledger, current_id).model_dump()),
        created_at=view.household.created_at,
        updated_at=view.household.updated_at,
    )


def _application(session: AsyncSession, settings: Settings) -> ClientApplicationService:
    return ClientApplicationService(session, currency=settings.currency, timezone=settings.timezone)


def _raise_account_error(exc: AccountError) -> None:
    if isinstance(exc, AccountNotFoundError):
        raise client_error(404, "resource_not_found", "resource not found") from exc
    if isinstance(exc, AccountConflictError):
        raise client_error(409, "conflict", str(exc)) from exc
    raise client_error(422, "validation_error", str(exc)) from exc


async def _idempotent(
    session: AsyncSession,
    principal: ClientPrincipal,
    *,
    operation: str,
    key: str | None,
    payload: Any,
    callback: Any,
    status_code: int = 200,
) -> tuple[dict[str, Any], bool]:
    if key is None or not key.strip() or len(key.strip()) > 128:
        raise client_error(
            422,
            "validation_error",
            "Idempotency-Key header must contain 1 to 128 characters",
        )
    try:
        return await ClientIdempotencyService(session).execute(
            principal.context,
            operation=operation,
            key=key,
            payload=jsonable_encoder(payload),
            callback=callback,
            response_status=status_code,
        )
    except IdempotencyConflictError as exc:
        await session.rollback()
        raise client_error(409, "conflict", str(exc)) from exc
    except IdempotencyInProgressError as exc:
        await session.rollback()
        raise client_error(503, "temporary_failure", str(exc)) from exc


@router.get("/me", response_model=ClientIdentity, responses=ERRORS)
async def me(
    request: Request,
    principal: Annotated[ClientPrincipal, Depends(client_principal)],
) -> ClientIdentity:
    _require(principal, "ledger:read")
    return ClientIdentity(
        user_id=str(principal.context.actor_user_id),
        display_name=principal.display_name,
        ledger_id=str(principal.context.ledger_id),
        source_channel=principal.context.source_channel,
        credential_id=str(principal.credential_id),
        scopes=sorted(principal.scopes),
    )


@router.get("/ledgers", response_model=ClientLedgerList, responses=ERRORS)
async def ledgers(
    request: Request,
    principal: Annotated[ClientPrincipal, Depends(client_principal)],
) -> ClientLedgerList:
    _require(principal, "ledger:read")
    settings = _settings(request)
    async with _factory(request)() as session:
        rows = await _application(session, settings).list_ledgers(principal.context)
    return ClientLedgerList(items=[_ledger(row, principal.context.ledger_id) for row in rows])


@router.get("/ledgers/current", response_model=ClientLedger, responses=ERRORS)
async def current_ledger(
    request: Request,
    principal: Annotated[ClientPrincipal, Depends(client_principal)],
) -> ClientLedger:
    _require(principal, "ledger:read")
    async with _factory(request)() as session:
        row = await _application(session, _settings(request)).current_ledger(principal.context)
    return _ledger(row, principal.context.ledger_id)


@router.post("/ledgers/{ledger_id}/select", response_model=ClientLedger, responses=ERRORS)
async def select_ledger(
    ledger_id: uuid.UUID,
    request: Request,
    principal: Annotated[ClientPrincipal, Depends(client_principal)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> ClientLedger:
    _require(principal, "ledger:write")
    async with _factory(request)() as session:
        try:
            row = await session.get(Ledger, ledger_id)
            if row is None:
                raise LedgerAuthorizationError
            target_context = principal.context.__class__(
                actor_user_id=principal.context.actor_user_id,
                ledger_id=ledger_id,
                source_channel=principal.context.source_channel,
                external_subject_id=principal.context.external_subject_id,
            )
            await _application(session, _settings(request)).authorize(target_context)

            async def apply(_: ClientIdempotencyRecord) -> dict[str, Any]:
                await ClientCredentialService.select_ledger(
                    session,
                    user_id=principal.context.actor_user_id,
                    credential_id=principal.credential_id,
                    ledger_id=ledger_id,
                )
                return _ledger(row, ledger_id).model_dump(mode="json")

            data, _ = await ClientIdempotencyService(session).execute(
                target_context,
                operation="ledger.select",
                key=idempotency_key or "",
                payload={"ledger_id": str(ledger_id)},
                callback=apply,
            )
            return ClientLedger.model_validate(data)
        except LedgerAuthorizationError as exc:
            await session.rollback()
            raise client_error(404, "resource_not_found", "resource not found") from exc
        except IdempotencyConflictError as exc:
            await session.rollback()
            raise client_error(409, "conflict", str(exc)) from exc
        except ValueError as exc:
            await session.rollback()
            raise client_error(422, "validation_error", str(exc)) from exc


@router.post("/ledgers", response_model=ClientLedger, status_code=201, responses=ERRORS)
async def create_ledger(
    payload: ClientLedgerNameRequest,
    request: Request,
    principal: Annotated[ClientPrincipal, Depends(client_principal)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> ClientLedger:
    _require(principal, "ledger:write")
    async with _factory(request)() as session:
        app = _application(session, _settings(request))

        async def apply(_: ClientIdempotencyRecord) -> dict[str, Any]:
            row = await app.create_personal_ledger(principal.context, payload.name)
            return _ledger(row, principal.context.ledger_id).model_dump(mode="json")

        try:
            data, _ = await _idempotent(
                session,
                principal,
                operation="ledger.create",
                key=idempotency_key,
                payload=payload,
                callback=apply,
                status_code=201,
            )
            return ClientLedger.model_validate(data)
        except LedgerManagementError as exc:
            raise client_error(409, "conflict", str(exc)) from exc


@router.patch("/ledgers/{ledger_id}", response_model=ClientLedger, responses=ERRORS)
async def rename_ledger(
    ledger_id: uuid.UUID,
    payload: ClientLedgerNameRequest,
    request: Request,
    principal: Annotated[ClientPrincipal, Depends(client_principal)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> ClientLedger:
    _require(principal, "ledger:write")
    async with _factory(request)() as session:
        app = _application(session, _settings(request))

        async def apply(_: ClientIdempotencyRecord) -> dict[str, Any]:
            row = await app.rename_personal_ledger(principal.context, ledger_id, payload.name)
            return _ledger(row, principal.context.ledger_id).model_dump(mode="json")

        try:
            data, _ = await _idempotent(
                session,
                principal,
                operation=f"ledger.rename:{ledger_id}",
                key=idempotency_key,
                payload=payload,
                callback=apply,
            )
            return ClientLedger.model_validate(data)
        except LedgerManagementError as exc:
            raise client_error(404, "resource_not_found", "resource not found") from exc


@router.post("/ledgers/{ledger_id}/default", response_model=ClientLedger, responses=ERRORS)
async def set_default_ledger(
    ledger_id: uuid.UUID,
    request: Request,
    principal: Annotated[ClientPrincipal, Depends(client_principal)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> ClientLedger:
    _require(principal, "ledger:write")
    async with _factory(request)() as session:
        app = _application(session, _settings(request))

        async def apply(_: ClientIdempotencyRecord) -> dict[str, Any]:
            row = await app.set_default_ledger(principal.context, ledger_id)
            return _ledger(row, principal.context.ledger_id).model_dump(mode="json")

        try:
            data, _ = await _idempotent(
                session,
                principal,
                operation=f"ledger.default:{ledger_id}",
                key=idempotency_key,
                payload={"ledger_id": str(ledger_id)},
                callback=apply,
            )
            return ClientLedger.model_validate(data)
        except LedgerManagementError as exc:
            raise client_error(404, "resource_not_found", "resource not found") from exc


@router.get("/households", response_model=HouseholdList, responses=ERRORS)
async def households(
    request: Request,
    principal: Annotated[ClientPrincipal, Depends(client_principal)],
) -> HouseholdList:
    _require(principal, "ledger:read")
    async with _factory(request)() as session:
        rows = await _application(session, _settings(request)).list_households(principal.context)
    return HouseholdList(items=[_household(row, principal.context.ledger_id) for row in rows])


@router.post("/households", response_model=WebHousehold, status_code=201, responses=ERRORS)
async def create_household(
    payload: HouseholdCreateRequest,
    request: Request,
    principal: Annotated[ClientPrincipal, Depends(client_principal)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> WebHousehold:
    _require(principal, "ledger:write")
    async with _factory(request)() as session:
        app = _application(session, _settings(request))

        async def apply(_: ClientIdempotencyRecord) -> dict[str, Any]:
            view = await app.create_household(principal.context, payload.name)
            return _household(view, principal.context.ledger_id).model_dump(mode="json")

        try:
            data, _ = await _idempotent(
                session,
                principal,
                operation="household.create",
                key=idempotency_key,
                payload=payload,
                callback=apply,
                status_code=201,
            )
            return WebHousehold.model_validate(data)
        except HouseholdManagementError as exc:
            raise client_error(409, "conflict", str(exc)) from exc


@router.get("/households/{household_id}", response_model=WebHousehold, responses=ERRORS)
async def household_detail(
    household_id: uuid.UUID,
    request: Request,
    principal: Annotated[ClientPrincipal, Depends(client_principal)],
) -> WebHousehold:
    _require(principal, "ledger:read")
    async with _factory(request)() as session:
        app = _application(session, _settings(request))
        try:
            view = await app.get_household(principal.context, household_id)
            members = await app.list_household_members(principal.context, household_id)
        except HouseholdManagementError as exc:
            raise client_error(404, "resource_not_found", "resource not found") from exc
    result = _household(view, principal.context.ledger_id)
    result.members = [
        WebHouseholdMember(
            user_id=str(item.user.id),
            display_name=item.user.display_name,
            role=item.membership.role,
            joined_at=item.membership.joined_at,
        )
        for item in members
    ]
    return result


@router.patch("/households/{household_id}", response_model=WebHousehold, responses=ERRORS)
async def rename_household(
    household_id: uuid.UUID,
    payload: HouseholdCreateRequest,
    request: Request,
    principal: Annotated[ClientPrincipal, Depends(client_principal)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> WebHousehold:
    _require(principal, "ledger:write")
    async with _factory(request)() as session:
        app = _application(session, _settings(request))

        async def apply(_: ClientIdempotencyRecord) -> dict[str, Any]:
            view = await app.rename_household(principal.context, household_id, payload.name)
            return _household(view, principal.context.ledger_id).model_dump(mode="json")

        try:
            data, _ = await _idempotent(
                session,
                principal,
                operation=f"household.rename:{household_id}",
                key=idempotency_key,
                payload=payload,
                callback=apply,
            )
            return WebHousehold.model_validate(data)
        except HouseholdManagementError as exc:
            raise client_error(404, "resource_not_found", "resource not found") from exc


@router.get(
    "/households/{household_id}/members",
    response_model=list[WebHouseholdMember],
    responses=ERRORS,
)
async def household_members(
    household_id: uuid.UUID,
    request: Request,
    principal: Annotated[ClientPrincipal, Depends(client_principal)],
) -> list[WebHouseholdMember]:
    detail = await household_detail(household_id, request, principal)
    return detail.members or []


@router.post(
    "/households/{household_id}/leave",
    response_model=ClientCommandResult,
    responses=ERRORS,
)
async def leave_household(
    household_id: uuid.UUID,
    request: Request,
    principal: Annotated[ClientPrincipal, Depends(client_principal)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> ClientCommandResult:
    _require(principal, "ledger:write")
    async with _factory(request)() as session:
        app = _application(session, _settings(request))

        async def apply(_: ClientIdempotencyRecord) -> dict[str, Any]:
            await app.leave_household(principal.context, household_id)
            return ClientCommandResult(message="household left").model_dump(mode="json")

        try:
            data, replayed = await _idempotent(
                session,
                principal,
                operation=f"household.leave:{household_id}",
                key=idempotency_key,
                payload={"household_id": str(household_id)},
                callback=apply,
            )
            data["replayed"] = replayed
            return ClientCommandResult.model_validate(data)
        except HouseholdManagementError as exc:
            raise client_error(404, "resource_not_found", "resource not found") from exc


@router.delete(
    "/households/{household_id}/members/{user_id}",
    response_model=ClientCommandResult,
    responses=ERRORS,
)
async def remove_household_member(
    household_id: uuid.UUID,
    user_id: uuid.UUID,
    request: Request,
    principal: Annotated[ClientPrincipal, Depends(client_principal)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> ClientCommandResult:
    _require(principal, "ledger:write")
    async with _factory(request)() as session:
        app = _application(session, _settings(request))

        async def apply(_: ClientIdempotencyRecord) -> dict[str, Any]:
            await app.remove_household_member(principal.context, household_id, user_id)
            session.add(
                ClientSecurityAudit(
                    actor_user_id=principal.context.actor_user_id,
                    credential_id=principal.credential_id,
                    action="household.member.remove",
                    outcome="succeeded",
                )
            )
            return ClientCommandResult(message="household member removed").model_dump(mode="json")

        try:
            data, replayed = await _idempotent(
                session,
                principal,
                operation=f"household.member.remove:{household_id}:{user_id}",
                key=idempotency_key,
                payload={"household_id": str(household_id), "user_id": str(user_id)},
                callback=apply,
            )
            data["replayed"] = replayed
            return ClientCommandResult.model_validate(data)
        except HouseholdManagementError as exc:
            raise client_error(404, "resource_not_found", "resource not found") from exc


@router.post(
    "/households/{household_id}/invitations",
    response_model=WebHouseholdInvitation,
    status_code=201,
    responses=ERRORS,
)
async def invite_household_member(
    household_id: uuid.UUID,
    payload: HouseholdInviteRequest,
    request: Request,
    principal: Annotated[ClientPrincipal, Depends(client_principal)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> WebHouseholdInvitation:
    _require(principal, "ledger:write")
    async with _factory(request)() as session:
        app = _application(session, _settings(request))

        async def apply(_: ClientIdempotencyRecord) -> dict[str, Any]:
            invitation = await app.invite_household_member(
                principal.context, household_id, payload.target
            )
            household = await session.get(Household, invitation.household_id)
            assert household is not None
            return WebHouseholdInvitation(
                id=str(invitation.id),
                invitation_code=invitation.public_id,
                household_id=str(invitation.household_id),
                household_name=household.name,
                target_user_id=str(invitation.target_user_id),
                status=invitation.status,
                expires_at=invitation.expires_at,
                created_at=invitation.created_at,
            ).model_dump(mode="json")

        try:
            data, _ = await _idempotent(
                session,
                principal,
                operation=f"household.invite:{household_id}",
                key=idempotency_key,
                payload=payload,
                callback=apply,
                status_code=201,
            )
            return WebHouseholdInvitation.model_validate(data)
        except HouseholdManagementError as exc:
            raise client_error(404, "resource_not_found", "resource not found") from exc


@router.get(
    "/household-invitations",
    response_model=list[WebHouseholdInvitation],
    responses=ERRORS,
)
async def household_invitations(
    request: Request,
    principal: Annotated[ClientPrincipal, Depends(client_principal)],
) -> list[WebHouseholdInvitation]:
    _require(principal, "ledger:read")
    async with _factory(request)() as session:
        invitations = await _application(session, _settings(request)).list_household_invitations(
            principal.context
        )
        result = []
        for invitation in invitations:
            household = await session.get(Household, invitation.household_id)
            result.append(
                WebHouseholdInvitation(
                    id=str(invitation.id),
                    invitation_code=invitation.public_id,
                    household_id=str(invitation.household_id),
                    household_name=household.name if household else "",
                    target_user_id=str(invitation.target_user_id),
                    status=invitation.status,
                    expires_at=invitation.expires_at,
                    created_at=invitation.created_at,
                )
            )
    return result


async def _respond_invitation(
    *,
    invitation_id: uuid.UUID,
    action: str,
    request: Request,
    principal: ClientPrincipal,
    idempotency_key: str | None,
) -> WebHouseholdInvitation:
    _require(principal, "ledger:write")
    async with _factory(request)() as session:
        app = _application(session, _settings(request))

        async def apply(_: ClientIdempotencyRecord) -> dict[str, Any]:
            invitation = await app.respond_household_invitation(
                principal.context, invitation_id, action
            )
            household = await session.get(Household, invitation.household_id)
            return WebHouseholdInvitation(
                id=str(invitation.id),
                invitation_code=invitation.public_id,
                household_id=str(invitation.household_id),
                household_name=household.name if household else "",
                target_user_id=str(invitation.target_user_id),
                status=invitation.status,
                expires_at=invitation.expires_at,
                created_at=invitation.created_at,
            ).model_dump(mode="json")

        try:
            data, _ = await _idempotent(
                session,
                principal,
                operation=f"household.invitation.{action}:{invitation_id}",
                key=idempotency_key,
                payload={"invitation_id": str(invitation_id), "action": action},
                callback=apply,
            )
            return WebHouseholdInvitation.model_validate(data)
        except HouseholdManagementError as exc:
            raise client_error(404, "resource_not_found", "resource not found") from exc


@router.post(
    "/household-invitations/{invitation_id}/accept",
    response_model=WebHouseholdInvitation,
    responses=ERRORS,
)
async def accept_household_invitation(
    invitation_id: uuid.UUID,
    request: Request,
    principal: Annotated[ClientPrincipal, Depends(client_principal)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> WebHouseholdInvitation:
    return await _respond_invitation(
        invitation_id=invitation_id,
        action="accept",
        request=request,
        principal=principal,
        idempotency_key=idempotency_key,
    )


@router.post(
    "/household-invitations/{invitation_id}/reject",
    response_model=WebHouseholdInvitation,
    responses=ERRORS,
)
async def reject_household_invitation(
    invitation_id: uuid.UUID,
    request: Request,
    principal: Annotated[ClientPrincipal, Depends(client_principal)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> WebHouseholdInvitation:
    return await _respond_invitation(
        invitation_id=invitation_id,
        action="reject",
        request=request,
        principal=principal,
        idempotency_key=idempotency_key,
    )


@router.post(
    "/household-invitations/{invitation_id}/cancel",
    response_model=WebHouseholdInvitation,
    responses=ERRORS,
)
async def cancel_household_invitation(
    invitation_id: uuid.UUID,
    request: Request,
    principal: Annotated[ClientPrincipal, Depends(client_principal)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> WebHouseholdInvitation:
    return await _respond_invitation(
        invitation_id=invitation_id,
        action="cancel",
        request=request,
        principal=principal,
        idempotency_key=idempotency_key,
    )


@router.get("/accounts", response_model=ClientAccountList, responses=ERRORS)
async def accounts(
    request: Request,
    principal: Annotated[ClientPrincipal, Depends(client_principal)],
    include_archived: bool = False,
) -> ClientAccountList:
    _require(principal, "ledger:read")
    async with _factory(request)() as session:
        rows = await _application(session, _settings(request)).list_accounts(
            principal.context, include_archived=include_archived
        )
    return ClientAccountList(items=[_account(row) for row in rows])


@router.get("/accounts/{account_id}", response_model=ClientAccount, responses=ERRORS)
async def account_detail(
    account_id: uuid.UUID,
    request: Request,
    principal: Annotated[ClientPrincipal, Depends(client_principal)],
) -> ClientAccount:
    _require(principal, "ledger:read")
    async with _factory(request)() as session:
        try:
            row = await _application(session, _settings(request)).get_account(
                principal.context, account_id
            )
        except AccountError as exc:
            _raise_account_error(exc)
    return _account(row)


@router.post("/accounts", response_model=ClientAccount, status_code=201, responses=ERRORS)
async def create_account(
    payload: ClientAccountCreateRequest,
    request: Request,
    principal: Annotated[ClientPrincipal, Depends(client_principal)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> ClientAccount:
    _require(principal, "ledger:write")
    async with _factory(request)() as session:
        app = _application(session, _settings(request))

        async def apply(_: ClientIdempotencyRecord) -> dict[str, Any]:
            row = await app.create_account(
                principal.context,
                name=payload.name,
                account_type=payload.type,
                subtype=payload.subtype,
                provider=payload.provider,
                currency=payload.currency,
                opening_balance=payload.opening_balance,
                make_default=payload.is_default,
            )
            return _account(row).model_dump(mode="json")

        try:
            data, _ = await _idempotent(
                session,
                principal,
                operation="account.create",
                key=idempotency_key,
                payload=payload,
                callback=apply,
                status_code=201,
            )
        except AccountError as exc:
            _raise_account_error(exc)
        return ClientAccount.model_validate(data)


async def _mutate_account(
    *,
    account_id: uuid.UUID,
    operation: str,
    payload: Any,
    request: Request,
    principal: ClientPrincipal,
    idempotency_key: str | None,
) -> ClientAccount:
    _require(principal, "ledger:write")
    async with _factory(request)() as session:
        app = _application(session, _settings(request))

        async def apply(_: ClientIdempotencyRecord) -> dict[str, Any]:
            if operation == "rename":
                row = await app.rename_account(principal.context, account_id, payload.name)
            elif operation == "archive":
                row = await app.archive_account(principal.context, account_id)
            else:
                row = await app.set_default_account(principal.context, account_id)
            return _account(row).model_dump(mode="json")

        try:
            data, _ = await _idempotent(
                session,
                principal,
                operation=f"account.{operation}:{account_id}",
                key=idempotency_key,
                payload=payload,
                callback=apply,
            )
        except AccountError as exc:
            _raise_account_error(exc)
        return ClientAccount.model_validate(data)


@router.patch("/accounts/{account_id}", response_model=ClientAccount, responses=ERRORS)
async def rename_account(
    account_id: uuid.UUID,
    payload: ClientAccountRenameRequest,
    request: Request,
    principal: Annotated[ClientPrincipal, Depends(client_principal)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> ClientAccount:
    return await _mutate_account(
        account_id=account_id,
        operation="rename",
        payload=payload,
        request=request,
        principal=principal,
        idempotency_key=idempotency_key,
    )


@router.post("/accounts/{account_id}/archive", response_model=ClientAccount, responses=ERRORS)
async def archive_account(
    account_id: uuid.UUID,
    request: Request,
    principal: Annotated[ClientPrincipal, Depends(client_principal)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> ClientAccount:
    return await _mutate_account(
        account_id=account_id,
        operation="archive",
        payload={"account_id": str(account_id)},
        request=request,
        principal=principal,
        idempotency_key=idempotency_key,
    )


@router.post("/accounts/{account_id}/default", response_model=ClientAccount, responses=ERRORS)
async def set_default_account(
    account_id: uuid.UUID,
    request: Request,
    principal: Annotated[ClientPrincipal, Depends(client_principal)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> ClientAccount:
    return await _mutate_account(
        account_id=account_id,
        operation="default",
        payload={"account_id": str(account_id)},
        request=request,
        principal=principal,
        idempotency_key=idempotency_key,
    )


@router.get("/dashboard", response_model=DashboardData, responses=ERRORS)
async def dashboard(
    request: Request,
    principal: Annotated[ClientPrincipal, Depends(client_principal)],
) -> DashboardData:
    _require(principal, "ledger:read")
    async with _factory(request)() as session:
        return await _application(session, _settings(request)).dashboard(principal.context)


@router.get("/entries", response_model=EntryPage, responses=ERRORS)
async def entries(
    request: Request,
    principal: Annotated[ClientPrincipal, Depends(client_principal)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
    start: datetime | None = None,
    end: datetime | None = None,
    deleted: DeletedFilter = "active",
    sort: EntrySort = "occurred_at",
    order: SortOrder = "desc",
) -> EntryPage:
    _require(principal, "ledger:read")
    if start is None:
        start = datetime.now(UTC) - timedelta(days=30)
    async with _factory(request)() as session:
        return await _application(session, _settings(request)).list_entries(
            principal.context,
            EntryQuery(
                page=page,
                page_size=page_size,
                start=start,
                end=end,
                direction=None,
                category=None,
                source_type=None,
                amount_min=None,
                amount_max=None,
                search=None,
                deleted=deleted,
                sort=sort,
                order=order,
            ),
        )


@router.post("/entries", response_model=ClientCommandResult, status_code=201, responses=ERRORS)
async def create_entry(
    payload: ClientEntryCreateRequest,
    request: Request,
    principal: Annotated[ClientPrincipal, Depends(client_principal)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> ClientCommandResult:
    _require(principal, "ledger:write")
    async with _factory(request)() as session:
        app = _application(session, _settings(request))

        async def apply(record: ClientIdempotencyRecord) -> dict[str, Any]:
            source_id = f"client:{record.id}"
            result = await app.execute_financial(
                principal.context,
                ParsedCommand(
                    action=Action.CREATE,
                    **payload.model_dump(exclude={"account_id"}),
                ),
                source_type="client_api",
                source_message_id=source_id,
                commit_changes=False,
                account_id=uuid.UUID(payload.account_id) if payload.account_id else None,
            )
            entry = await session.scalar(
                select(LedgerEntry).where(
                    LedgerEntry.ledger_id == principal.context.ledger_id,
                    LedgerEntry.source_message_id == source_id,
                )
            )
            resource = (
                {
                    "id": str(entry.id),
                    "short_id": entry.short_id,
                    "account_id": str(entry.account_id),
                }
                if entry is not None
                else None
            )
            return ClientCommandResult(message=result.message, resource=resource).model_dump(
                mode="json"
            )

        try:
            data, replayed = await _idempotent(
                session,
                principal,
                operation="entry.create",
                key=idempotency_key,
                payload=payload,
                callback=apply,
                status_code=201,
            )
        except AccountError as exc:
            await session.rollback()
            _raise_account_error(exc)
        except ValueError as exc:
            await session.rollback()
            raise client_error(422, "validation_error", str(exc)) from exc
        data["replayed"] = replayed
        return ClientCommandResult.model_validate(data)


@router.get("/entries/{short_id}", response_model=EntryDetail, responses=ERRORS)
async def entry_detail(
    short_id: str,
    request: Request,
    principal: Annotated[ClientPrincipal, Depends(client_principal)],
) -> EntryDetail:
    _require(principal, "ledger:read")
    async with _factory(request)() as session:
        try:
            detail = await _application(session, _settings(request)).entry_detail(
                principal.context, short_id
            )
        except ValueError as exc:
            raise client_error(404, "resource_not_found", "resource not found") from exc
    if detail is None:
        raise client_error(404, "resource_not_found", "resource not found")
    return detail


async def _mutate_entry(
    *,
    short_id: str,
    command: ParsedCommand,
    expected_updated_at: datetime,
    request: Request,
    principal: ClientPrincipal,
    idempotency_key: str | None,
) -> EntryDetail:
    async with _factory(request)() as session:
        app = _application(session, _settings(request))

        async def apply(_: ClientIdempotencyRecord) -> dict[str, Any]:
            existing = await app.entry_detail(principal.context, short_id)
            if existing is None:
                raise LookupError
            await app.execute_financial(
                principal.context,
                command,
                source_type="client_api",
                expected_updated_at=expected_updated_at,
                commit_changes=False,
            )
            detail = await app.entry_detail(principal.context, short_id)
            if detail is None:
                raise LookupError
            return detail.model_dump(mode="json")

        try:
            data, _ = await _idempotent(
                session,
                principal,
                operation=f"entry.{command.action.value}:{short_id}",
                key=idempotency_key,
                payload=command.model_dump(mode="json")
                | {"expected_updated_at": expected_updated_at.isoformat()},
                callback=apply,
            )
            return EntryDetail.model_validate(data)
        except (LookupError, ValueError) as exc:
            await session.rollback()
            raise client_error(404, "resource_not_found", "resource not found") from exc
        except EntryConflictError as exc:
            await session.rollback()
            raise client_error(409, "conflict", "entry was modified by another request") from exc


@router.patch("/entries/{short_id}", response_model=EntryDetail, responses=ERRORS)
async def update_entry(
    short_id: str,
    payload: EntryUpdateRequest,
    request: Request,
    principal: Annotated[ClientPrincipal, Depends(client_principal)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> EntryDetail:
    _require(principal, "ledger:write")
    command = ParsedCommand(
        action=Action.UPDATE_ENTRY,
        entry_ref=short_id,
        amount=payload.amount,
        direction=payload.direction,
        category=payload.category,
        note=payload.note,
        occurred_at=payload.occurred_at,
        clear_note=payload.note == "",
    )
    return await _mutate_entry(
        short_id=short_id,
        command=command,
        expected_updated_at=payload.expected_updated_at,
        request=request,
        principal=principal,
        idempotency_key=idempotency_key,
    )


@router.delete("/entries/{short_id}", response_model=EntryDetail, responses=ERRORS)
async def delete_entry(
    short_id: str,
    payload: EntryVersionRequest,
    request: Request,
    principal: Annotated[ClientPrincipal, Depends(client_principal)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> EntryDetail:
    _require(principal, "ledger:write")
    return await _mutate_entry(
        short_id=short_id,
        command=ParsedCommand(action=Action.DELETE_ENTRY, entry_ref=short_id),
        expected_updated_at=payload.expected_updated_at,
        request=request,
        principal=principal,
        idempotency_key=idempotency_key,
    )


@router.post("/entries/{short_id}/restore", response_model=EntryDetail, responses=ERRORS)
async def restore_entry(
    short_id: str,
    payload: EntryVersionRequest,
    request: Request,
    principal: Annotated[ClientPrincipal, Depends(client_principal)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> EntryDetail:
    _require(principal, "ledger:write")
    return await _mutate_entry(
        short_id=short_id,
        command=ParsedCommand(action=Action.RESTORE_ENTRY, entry_ref=short_id),
        expected_updated_at=payload.expected_updated_at,
        request=request,
        principal=principal,
        idempotency_key=idempotency_key,
    )


@router.get("/budgets", response_model=BudgetOverview, responses=ERRORS)
async def budgets(
    request: Request,
    principal: Annotated[ClientPrincipal, Depends(client_principal)],
) -> BudgetOverview:
    _require(principal, "ledger:read")
    async with _factory(request)() as session:
        return await _application(session, _settings(request)).budgets(principal.context)


@router.put("/budgets/{category}", response_model=BudgetOverview, responses=ERRORS)
async def update_budget(
    category: str,
    payload: BudgetUpdateRequest,
    request: Request,
    principal: Annotated[ClientPrincipal, Depends(client_principal)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> BudgetOverview:
    _require(principal, "ledger:write")
    async with _factory(request)() as session:
        app = _application(session, _settings(request))

        async def apply(_: ClientIdempotencyRecord) -> dict[str, Any]:
            await app.execute_financial(
                principal.context,
                ParsedCommand(
                    action=Action.SET_BUDGET,
                    category=category,
                    amount=payload.amount,
                    currency=payload.currency,
                ),
                source_type="client_api",
                commit_changes=False,
            )
            return (await app.budgets(principal.context)).model_dump(mode="json")

        try:
            data, _ = await _idempotent(
                session,
                principal,
                operation=f"budget.set:{category}",
                key=idempotency_key,
                payload=payload,
                callback=apply,
            )
        except ValueError as exc:
            await session.rollback()
            raise client_error(422, "validation_error", str(exc)) from exc
        return BudgetOverview.model_validate(data)


@router.delete("/budgets/{category}", response_model=BudgetOverview, responses=ERRORS)
async def delete_budget(
    category: str,
    request: Request,
    principal: Annotated[ClientPrincipal, Depends(client_principal)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> BudgetOverview:
    _require(principal, "ledger:write")
    async with _factory(request)() as session:
        app = _application(session, _settings(request))

        async def apply(_: ClientIdempotencyRecord) -> dict[str, Any]:
            await app.execute_financial(
                principal.context,
                ParsedCommand(action=Action.DELETE_BUDGET, category=category),
                source_type="client_api",
                commit_changes=False,
            )
            return (await app.budgets(principal.context)).model_dump(mode="json")

        try:
            data, _ = await _idempotent(
                session,
                principal,
                operation=f"budget.delete:{category}",
                key=idempotency_key,
                payload={"category": category},
                callback=apply,
            )
        except ValueError as exc:
            await session.rollback()
            raise client_error(422, "validation_error", str(exc)) from exc
        return BudgetOverview.model_validate(data)


@router.get("/analytics", response_model=AnalyticsOverview, responses=ERRORS)
async def analytics(
    request: Request,
    principal: Annotated[ClientPrincipal, Depends(client_principal)],
    start_date: date,
    end_date: date,
) -> AnalyticsOverview:
    _require(principal, "ledger:read")
    async with _factory(request)() as session:
        summary, trend, categories, monthly = await _application(
            session, _settings(request)
        ).analytics(principal.context, start_date=start_date, end_date=end_date)
    return AnalyticsOverview(summary=summary, trend=trend, categories=categories)


@router.get("/reports", response_model=ReportData, responses=ERRORS)
async def report(
    request: Request,
    principal: Annotated[ClientPrincipal, Depends(client_principal)],
    start: datetime,
    end: datetime,
) -> ReportData:
    _require(principal, "ledger:read")
    async with _factory(request)() as session:
        result = await _application(session, _settings(request)).execute_financial(
            principal.context,
            ParsedCommand(action=Action.REPORT, range_start=start, range_end=end),
            source_type="client_api",
            commit_changes=False,
        )
    if result.report is None:
        raise client_error(503, "temporary_failure", "report unavailable")
    return result.report


@router.get("/exports.csv", responses=ERRORS)
async def export_csv(
    request: Request,
    principal: Annotated[ClientPrincipal, Depends(client_principal)],
    start: datetime | None = None,
    end: datetime | None = None,
    include_deleted: bool = False,
) -> Response:
    _require(principal, "ledger:read")
    async with _factory(request)() as session:
        result = await _application(session, _settings(request)).execute_financial(
            principal.context,
            ParsedCommand(
                action=Action.EXPORT_ENTRIES,
                range_start=start,
                range_end=end,
                include_deleted=include_deleted,
            ),
            source_type="client_api",
            commit_changes=False,
        )
    if result.export is None:
        raise client_error(503, "temporary_failure", "export unavailable")
    return Response(
        content=result.export.content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{result.export.filename}"'},
    )


@router.get("/pending", response_model=PendingPage, responses=ERRORS)
async def pending_list(
    request: Request,
    principal: Annotated[ClientPrincipal, Depends(client_principal)],
    group: PendingGroup = "pending",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=50)] = 20,
) -> PendingPage:
    _require(principal, "ledger:read")
    async with _factory(request)() as session:
        return await _application(session, _settings(request)).list_pending(
            principal.context, group=group, page=page, page_size=page_size
        )


@router.get("/pending/{confirmation_id}", response_model=PendingDetail, responses=ERRORS)
async def pending_detail(
    confirmation_id: str,
    request: Request,
    principal: Annotated[ClientPrincipal, Depends(client_principal)],
) -> PendingDetail:
    _require(principal, "ledger:read")
    async with _factory(request)() as session:
        try:
            detail = await _application(session, _settings(request)).pending_detail(
                principal.context, confirmation_id
            )
        except ConfirmationCodeError as exc:
            raise client_error(404, "resource_not_found", "resource not found") from exc
    if detail is None:
        raise client_error(404, "resource_not_found", "resource not found")
    return detail


async def _pending_action(
    *,
    action: str,
    confirmation_id: str,
    request: Request,
    principal: ClientPrincipal,
    idempotency_key: str | None,
) -> PendingActionResponse:
    _require(principal, "pending:write")
    if principal.context.external_subject_id is None:
        raise client_error(404, "resource_not_found", "resource not found")
    try:
        code = normalize_confirmation_code(confirmation_id)
    except ConfirmationCodeError as exc:
        raise client_error(404, "resource_not_found", "resource not found") from exc
    settings = _settings(request)
    factory = _factory(request)
    processor = getattr(request.app.state, "processor", None)
    store = cast(
        PendingCommandStore,
        getattr(processor, "_pending_store", None) or PendingCommandStore(factory, settings),
    )
    row = await store.get_by_code(principal.context.external_subject_id, code)
    if row is None or row.actor_user_id != principal.context.actor_user_id:
        raise client_error(404, "resource_not_found", "resource not found")
    if row.ledger_id != principal.context.ledger_id or not row.source_message_id:
        raise client_error(404, "resource_not_found", "resource not found")
    async with factory() as session:

        async def apply(_: ClientIdempotencyRecord) -> dict[str, Any]:
            now = datetime.now(UTC)
            if action == "confirm":
                message, outbox = await store.confirm_and_execute(
                    user_open_id=principal.context.external_subject_id or "",
                    confirmation_code=code,
                    reply_to_message_id=row.source_message_id or "",
                    confirm_event_id=None,
                    exchange_rates=getattr(processor, "exchange_rates", None),
                    now=now,
                )
            else:
                message, outbox = await store.cancel(
                    user_open_id=principal.context.external_subject_id or "",
                    confirmation_code=code,
                    reply_to_message_id=row.source_message_id or "",
                    cancel_event_id=None,
                    now=now,
                )
            if processor is not None:
                await processor._signal_or_deliver(outbox)
            session.add(
                ClientSecurityAudit(
                    actor_user_id=principal.context.actor_user_id,
                    credential_id=principal.credential_id,
                    action=f"pending.{action}",
                    outcome="succeeded",
                )
            )
            async with factory() as read_session:
                detail = await _application(read_session, settings).pending_detail(
                    principal.context, code
                )
            if detail is None:
                raise LookupError
            return PendingActionResponse(message=message, pending=detail).model_dump(mode="json")

        try:
            data, _ = await _idempotent(
                session,
                principal,
                operation=f"pending.{action}:{code}",
                key=idempotency_key,
                payload={"confirmation_id": code, "action": action},
                callback=apply,
            )
            return PendingActionResponse.model_validate(data)
        except LookupError as exc:
            raise client_error(404, "resource_not_found", "resource not found") from exc


@router.post(
    "/pending/{confirmation_id}/confirm",
    response_model=PendingActionResponse,
    responses=ERRORS,
)
async def confirm_pending(
    confirmation_id: str,
    request: Request,
    principal: Annotated[ClientPrincipal, Depends(client_principal)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> PendingActionResponse:
    return await _pending_action(
        action="confirm",
        confirmation_id=confirmation_id,
        request=request,
        principal=principal,
        idempotency_key=idempotency_key,
    )


@router.post(
    "/pending/{confirmation_id}/cancel",
    response_model=PendingActionResponse,
    responses=ERRORS,
)
async def cancel_pending(
    confirmation_id: str,
    request: Request,
    principal: Annotated[ClientPrincipal, Depends(client_principal)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> PendingActionResponse:
    return await _pending_action(
        action="cancel",
        confirmation_id=confirmation_id,
        request=request,
        principal=principal,
        idempotency_key=idempotency_key,
    )
