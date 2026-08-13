"""P42 — runtime build identity contract (unit level).

Proves:

* version / git_sha / build_time resolve from settings with safe fallbacks;
* the ``/version`` endpoint returns the public contract and never secrets;
* buildinfo never shells out to git and never inspects the filesystem for a
  repository.
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import FastAPI

from lark_ledger import __version__
from lark_ledger.api import router
from lark_ledger.buildinfo import (
    BUILD_INFO_FIELDS,
    UNKNOWN_GIT_SHA,
    BuildInfo,
    resolve_build_info,
)
from lark_ledger.config import Settings


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"_env_file": None}
    values.update(overrides)
    return Settings(**values)


async def get_version(app: FastAPI) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/version")


def test_build_info_uses_injected_build_metadata() -> None:
    info = resolve_build_info(
        _settings(version="0.11.0", git_sha="abc123def", build_time="2026-08-20T12:00:00Z")
    )

    assert info.version == "0.11.0"
    assert info.git_sha == "abc123def"
    assert info.build_time == "2026-08-20T12:00:00Z"


def test_build_info_falls_back_to_package_version_and_sentinels() -> None:
    info = resolve_build_info(_settings())

    assert info.version == __version__
    assert info.git_sha == UNKNOWN_GIT_SHA
    assert info.build_time == ""


def test_build_info_trims_whitespace_only_injected_values() -> None:
    info = resolve_build_info(_settings(version="  ", git_sha="  ", build_time="  "))

    assert info.version == __version__
    assert info.git_sha == UNKNOWN_GIT_SHA
    assert info.build_time == ""


def test_build_info_contract_fields_are_stable() -> None:
    assert BUILD_INFO_FIELDS == ("version", "git_sha", "build_time")
    assert BuildInfo("1.0.0", "sha", "t").to_dict() == {
        "version": "1.0.0",
        "git_sha": "sha",
        "build_time": "t",
    }


async def test_version_endpoint_returns_public_contract() -> None:
    app = FastAPI()
    app.include_router(router)
    app.state.settings = _settings(version="0.11.0", git_sha="abc123", build_time="t0")

    response = await get_version(app)

    assert response.status_code == 200
    assert response.json() == {
        "version": "0.11.0",
        "git_sha": "abc123",
        "build_time": "t0",
    }


async def test_version_endpoint_never_leaks_secrets() -> None:
    app = FastAPI()
    app.include_router(router)
    app.state.settings = _settings(
        database_url="postgresql+asyncpg://operator:hunter2@private-db.example/ledger",
        lark_app_secret="feishu-super-secret",
        ai_api_key="sk-secret-key-123",
        dashboard_session_secret="session-secret-value-1234567890",
        version="0.11.0",
        git_sha="abc123",
    )

    response = await get_version(app)
    rendered = response.text

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"version", "git_sha", "build_time"}
    for secret in (
        "hunter2",
        "private-db",
        "feishu-super-secret",
        "sk-secret-key-123",
        "session-secret-value-1234567890",
    ):
        assert secret not in rendered


async def test_version_endpoint_works_before_lifespan() -> None:
    app = FastAPI()
    app.include_router(router)

    response = await get_version(app)

    assert response.status_code == 200
    assert response.json()["version"] == __version__


def test_buildinfo_source_never_invokes_git_or_filesystem() -> None:
    import pathlib

    source = pathlib.Path("src/lark_ledger/buildinfo.py").read_text(encoding="utf-8")
    for banned in ("subprocess", "os.system", "git rev-parse", "Path("):
        assert banned not in source, f"buildinfo must not {banned}"
