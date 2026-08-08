"""Authenticated API boundary for the optional Web Dashboard."""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lark_ledger.config import Settings
from lark_ledger.services.dashboard_auth import (
    CSRF_COOKIE,
    CSRF_HEADER,
    OAUTH_COOKIE,
    SESSION_COOKIE,
    DashboardAuthError,
    DashboardAuthService,
    DashboardPrincipal,
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
