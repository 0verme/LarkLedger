"""P37 — OpenAPI contract for the human session endpoints.

Guards the ``/api/web/v1/auth/*`` surface: every endpoint exists, session
responses never expose credential material (no digest / raw secret / cookie
value), and the machine ``/api/v1`` Bearer contract stays intact.
"""

from __future__ import annotations

from typing import Any

from lark_ledger.config import Settings
from lark_ledger.main import create_app


def _schema() -> dict[str, Any]:
    app = create_app(
        Settings(
            _env_file=None,
            dashboard_enabled=True,
            dashboard_base_url="http://ledger.test",
            dashboard_session_secret="openapi-test-secret-long-enough-123456",
            dashboard_cookie_secure=False,
            lark_app_id="cli_test",
            lark_app_secret="app-secret",
        )
    )
    return app.openapi()


#: Every P37 session endpoint must exist.
REQUIRED_PATHS: dict[str, set[str]] = {
    "/api/web/v1/auth/session": {"get"},
    "/api/web/v1/auth/sessions": {"get"},
    "/api/web/v1/auth/sessions/{session_id}": {"delete"},
    "/api/web/v1/auth/sessions/revoke-others": {"post"},
    "/api/web/v1/auth/logout": {"post"},
    "/api/web/v1/auth/login": {"get"},
    "/api/web/v1/auth/callback": {"get"},
    "/api/web/v1/me": {"get"},
}

#: Field names that must never appear in a session response schema.
BANNED_RESPONSE_FIELDS = {
    "token",
    "digest",
    "cookie",
    "secret",
    "csrf",
    "lls1",
}


def test_session_endpoints_exist_in_openapi() -> None:
    schema = _schema()
    paths = schema["paths"]
    for path, methods in REQUIRED_PATHS.items():
        assert path in paths, f"missing session path {path}"
        for method in methods:
            assert method in paths[path], f"{path} missing {method.upper()}"


def _walk_schema_fields(node: Any, found: set[str], banned: set[str]) -> list[str]:
    """Collect any schema field that leaks banned credential names."""
    leaks: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str) and any(b in key.lower() for b in banned):
                leaks.append(key)
            leaks.extend(_walk_schema_fields(value, found, banned))
    elif isinstance(node, list):
        for item in node:
            leaks.extend(_walk_schema_fields(item, found, banned))
    return leaks


def test_session_responses_never_expose_credential_material() -> None:
    schema = _schema()
    leaks: list[str] = []
    for path, operations in schema["paths"].items():
        if not path.startswith("/api/web/v1/auth/") and path != "/api/web/v1/me":
            continue
        for operation in operations.values():
            if not isinstance(operation, dict):
                continue
            for response in operation.get("responses", {}).values():
                content = response.get("content", {})
                for media in content.values():
                    ref = media.get("schema", {}).get("$ref", "")
                    leaks.extend(
                        _walk_schema_fields(
                            media.get("schema", {}), set(), BANNED_RESPONSE_FIELDS
                        )
                    )
                    if ref:
                        name = ref.rsplit("/", 1)[-1]
                        component = schema.get("components", {}).get("schemas", {}).get(name, {})
                        leaks.extend(_walk_schema_fields(component, set(), BANNED_RESPONSE_FIELDS))
    assert leaks == [], f"session responses leak credential fields: {sorted(set(leaks))}"


def test_session_response_schema_shape() -> None:
    """The sessions list returns the documented safe projection."""
    schema = _schema()
    schemas = schema["components"]["schemas"]
    session_list = schemas.get("SessionList")
    assert session_list is not None, "SessionList schema missing from OpenAPI"
    props = session_list.get("properties", {})
    item_ref = props.get("items", {}).get("items", {}).get("$ref", "")
    assert item_ref.endswith("WebSession")
    web_session = schemas.get("WebSession", {})
    session_props = set(web_session.get("properties", {}).keys())
    assert {"id", "created_at", "last_seen_at", "expires_at", "current", "device"} <= session_props


def test_api_v1_bearer_scheme_unchanged() -> None:
    """The llv1_ machine contract must remain intact after P37."""
    schema = _schema()
    schemes = schema["components"]["securitySchemes"]
    assert schemes["clientBearer"]["type"] == "http"
    assert schemes["clientBearer"]["scheme"] == "bearer"
    assert schemes["clientBearer"]["bearerFormat"] == "llv1_"
    for path, operations in schema["paths"].items():
        if path.startswith("/api/v1/"):
            for operation in operations.values():
                assert operation.get("security") == [{"clientBearer": []}]
