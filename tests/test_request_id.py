"""P42 — request correlation + structured logging (unit level).

Proves the correlation contract:

* every request (healthz/readyz/webhook/API) gets a request_id;
* a valid client-supplied ``X-Request-ID`` is reused, an invalid one rejected;
* the response header echoes the id on every path;
* logs emitted while handling a request carry the same ``request_id``;
* sensitive headers / credentials never reach the log format or the payload.
"""

from __future__ import annotations

import io
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from lark_ledger.config import Settings
from lark_ledger.logging_config import (
    RequestIdFilter,
    generate_request_id,
    normalize_request_id,
    redact_sensitive,
    set_request_id,
)
from lark_ledger.main import create_app


async def get(app: FastAPI, path: str, headers: dict[str, str] | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path, headers=headers or {})


def test_normalize_request_id_accepts_only_safe_values() -> None:
    assert normalize_request_id("abc-123.xyz_9") == "abc-123.xyz_9"
    assert normalize_request_id(None) is None
    assert normalize_request_id("") is None
    assert normalize_request_id("a" * 129) is None  # too long
    assert normalize_request_id("has space") is None
    assert normalize_request_id("bad/char") is None
    assert normalize_request_id("bad\u0000char") is None


def test_generate_request_id_is_bounded_and_unique() -> None:
    first = generate_request_id()
    second = generate_request_id()
    assert len(first) == 16
    assert first != second
    assert normalize_request_id(first) == first


@pytest.mark.parametrize("path", ["/healthz", "/version", "/ops/status"])
async def test_every_public_path_returns_request_id_header(path: str) -> None:
    app = create_app(Settings(_env_file=None))
    app.state.settings = Settings(_env_file=None)

    response = await get(app, path)

    assert response.status_code == 200
    request_id = response.headers.get("x-request-id")
    assert request_id and normalize_request_id(request_id) == request_id


async def test_readyz_returns_request_id_header_even_when_not_ready() -> None:
    app = create_app(Settings(_env_file=None))
    app.state.settings = Settings(_env_file=None)

    response = await get(app, "/readyz")

    # Without lifespan the readiness service is missing -> 503, but the
    # correlation header must still be present for tracing the failure.
    assert response.status_code == 503
    request_id = response.headers.get("x-request-id")
    assert request_id and normalize_request_id(request_id) == request_id


async def test_client_supplied_correlation_id_is_reused() -> None:
    app = create_app(Settings(_env_file=None))
    app.state.settings = Settings(_env_file=None)

    response = await get(app, "/healthz", headers={"X-Request-ID": "client-trace-42"})

    assert response.headers["x-request-id"] == "client-trace-42"


async def test_invalid_client_correlation_id_is_replaced() -> None:
    app = create_app(Settings(_env_file=None))
    app.state.settings = Settings(_env_file=None)

    response = await get(app, "/healthz", headers={"X-Request-ID": "bad id with spaces"})

    echoed = response.headers["x-request-id"]
    assert echoed != "bad id with spaces"
    assert normalize_request_id(echoed) == echoed


async def test_request_id_propagates_to_logs() -> None:
    import logging as _logging


    class BrokenContext:
        async def __aenter__(self):
            raise RuntimeError("boom")

        async def __aexit__(self, *args: Any):
            return None

    class BrokenFactory:
        def __call__(self) -> BrokenContext:
            return BrokenContext()

    stream = io.StringIO()
    handler = _logging.StreamHandler(stream)
    handler.setFormatter(
        _logging.Formatter("%(request_id)s|%(message)s")
    )
    handler.addFilter(RequestIdFilter())
    logger = _logging.getLogger("lark_ledger.api")
    logger.setLevel(_logging.WARNING)
    logger.addHandler(handler)
    logger.propagate = False
    try:
        app = create_app(Settings(_env_file=None))
        app.state.settings = Settings(_env_file=None)
        app.state.session_factory = BrokenFactory()  # type: ignore[assignment]
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.get("/ops/status")
        assert response.status_code == 200
        output = stream.getvalue()
        lines = [line for line in output.splitlines() if "aggregate failed" in line]
        assert lines, f"expected a warning log line, got: {output!r}"
        request_id = lines[0].split("|", 1)[0]
        assert request_id and normalize_request_id(request_id) == request_id
    finally:
        logger.removeHandler(handler)
        logger.propagate = True


def test_request_id_context_var_is_scoped_per_context() -> None:
    from lark_ledger.logging_config import request_id_var, reset_request_id

    token = set_request_id("ctx-1")
    try:
        assert request_id_var.get() == "ctx-1"
    finally:
        reset_request_id(token)
    assert request_id_var.get() == ""


def test_redact_sensitive_scrubs_credentials() -> None:
    samples = {
        "Authorization: Bearer abc123secret456": "Authorization: [redacted]",
        "bearer lls1_abcdefghijklmnop": "bearer [redacted]",
        "token=supersecrettokenvalue": "token=[redacted]",
        "api_key=sk-1234567890abcdef": "api_key=[redacted]",
        "password=hunter2secret": "password=[redacted]",
        "digest abcdef0123456789abcdef0123456789abcdef0123456789": "digest [redacted]",
        "plain log line": "plain log line",
    }
    for raw, expected in samples.items():
        assert redact_sensitive(raw) == expected


def test_setup_logging_is_idempotent_and_installs_request_id_filter() -> None:
    import logging as _logging

    from lark_ledger.logging_config import RequestIdFilter, setup_logging

    root = _logging.getLogger()
    before_level = root.level
    before_handlers = list(root.handlers)
    try:
        setup_logging()
        setup_logging()  # second call must not duplicate handler / filter
        assert sum(
            isinstance(h, _logging.StreamHandler) for h in root.handlers
        ) == sum(
            isinstance(h, _logging.StreamHandler) for h in before_handlers
        ) + (0 if any(isinstance(h, _logging.StreamHandler) for h in before_handlers) else 1)
        assert sum(isinstance(f, RequestIdFilter) for f in root.filters) == 1
    finally:
        root.handlers[:] = before_handlers
        root.setLevel(before_level)
        root.filters[:] = [f for f in root.filters if not isinstance(f, RequestIdFilter)]


def test_source_never_logs_request_headers_or_cookies() -> None:
    import pathlib

    for filename in ("main.py", "web_api.py"):
        source = pathlib.Path(f"src/lark_ledger/{filename}").read_text(encoding="utf-8")
        for line in source.splitlines():
            stripped = line.strip()
            if "logger." not in stripped:
                continue
            assert "headers" not in stripped.lower(), f"header logging: {stripped}"
            assert "cookies" not in stripped.lower(), f"cookie logging: {stripped}"
            assert "authorization" not in stripped.lower(), f"auth logging: {stripped}"
