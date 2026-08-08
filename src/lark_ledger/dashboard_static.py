"""SPA static-file serving without shadowing existing API routes."""

from __future__ import annotations

from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

SECURITY_HEADERS = {
    b"content-security-policy": (
        b"default-src 'self'; script-src 'self'; style-src 'self'; "
        b"style-src-attr 'unsafe-inline'; "
        b"img-src 'self' data: https:; connect-src 'self'; object-src 'none'; "
        b"base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
    ),
    b"permissions-policy": b"camera=(), microphone=(), geolocation=()",
    b"referrer-policy": b"same-origin",
    b"x-content-type-options": b"nosniff",
    b"x-frame-options": b"DENY",
}


class DashboardSecurityHeaders:
    """Add browser hardening headers without changing bot-only deployments."""

    def __init__(self, app: ASGIApp, *, hsts: bool) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                for name, value in SECURITY_HEADERS.items():
                    if name not in present:
                        headers.append((name, value))
                if self.hsts and b"strict-transport-security" not in present:
                    headers.append(
                        (b"strict-transport-security", b"max-age=31536000; includeSubDomains")
                    )
                if str(scope.get("path", "")).startswith("/api/web/v1"):
                    headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


class DashboardStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404 or "." in path.rsplit("/", 1)[-1]:
                raise
            return await super().get_response("index.html", scope)
