"""OpenAPI contract tests for the channel-neutral Client API (v0.9.0).

The ``/api/v1`` surface is a stable contract from v0.9.0 onward. These tests
guard the schema shape (paths, auth, error components) so a refactor cannot
silently break the documented contract. They are structural — intentionally
not a full snapshot, which would churn on every unrelated schema tweak.
"""

from __future__ import annotations

from typing import Any

from lark_ledger.config import Settings
from lark_ledger.main import create_app


def _schema() -> dict[str, Any]:
    app = create_app(Settings(_env_file=None))
    return app.openapi()


#: The v0.9.0 §22 minimal surface — every one must exist under /api/v1.
REQUIRED_PATHS: dict[str, set[str]] = {
    "/api/v1/me": {"get"},
    "/api/v1/ledgers": {"get"},
    "/api/v1/ledgers/{ledger_id}": {"get"},
    "/api/v1/accounts": {"get", "post"},
    "/api/v1/transactions": {"get", "post"},
    "/api/v1/transactions/{entry_id}": {"get"},
    "/api/v1/transfers": {"post"},
    "/api/v1/budgets": {"get"},
    "/api/v1/recurring-rules": {"get"},
    "/api/v1/goals": {"get"},
    "/api/v1/overview": {"get"},
    "/api/v1/insights": {"get"},
}


def test_api_v1_required_surface_exists() -> None:
    schema = _schema()
    paths = schema["paths"]
    for path, methods in REQUIRED_PATHS.items():
        assert path in paths, f"missing /api/v1 path {path}"
        for method in methods:
            assert method in paths[path], f"{path} missing {method.upper()}"


def test_api_v1_and_legacy_prefix_surfaces_match() -> None:
    """Both prefixes serve the same handler set (minus nothing)."""
    schema = _schema()
    paths = schema["paths"]
    v1 = {p.removeprefix("/api/v1") for p in paths if p.startswith("/api/v1/")}
    legacy = {p.removeprefix("/api/client/v1") for p in paths if p.startswith("/api/client/v1/")}
    assert v1 == legacy, f"prefix mismatch: v1-only={v1 - legacy} legacy-only={legacy - v1}"


def test_api_v1_bearer_security_is_declared() -> None:
    schema = _schema()
    schemes = schema["components"]["securitySchemes"]
    assert "clientBearer" in schemes
    assert schemes["clientBearer"]["type"] == "http"
    assert schemes["clientBearer"]["scheme"] == "bearer"
    post = schema["paths"]["/api/v1/transactions"]["post"]
    assert post["security"] == [{"clientBearer": []}]


def test_api_v1_error_schema_is_stable() -> None:
    schema = _schema()
    components = schema["components"]["schemas"]
    assert "ClientErrorResponse" in components
    assert "ClientErrorDetail" in components
    detail = components["ClientErrorDetail"]
    props = detail.get("properties", {})
    assert "code" in props and "message" in props and "request_id" in props
    required = detail.get("required", [])
    assert "code" in required and "message" in required


def test_transaction_create_request_is_platform_neutral() -> None:
    """POST /api/v1/transactions body must be the neutral entry DTO."""
    schema = _schema()
    post = schema["paths"]["/api/v1/transactions"]["post"]
    ref = post["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    assert ref.endswith("/ClientEntryCreateRequest")
    props = schema["components"]["schemas"]["ClientEntryCreateRequest"]["properties"]
    assert "amount" in props and "direction" in props and "category" in props
    # No channel-specific fields may appear in the v1 write contract.
    for forbidden in ("open_id", "message_id", "chat_id", "cookie"):
        assert forbidden not in props


def test_write_endpoints_declare_idempotency_header() -> None:
    schema = _schema()
    for path in ("/api/v1/transactions", "/api/v1/transfers"):
        post = schema["paths"][path]["post"]
        params = post.get("parameters", [])
        names = {p.get("name") for p in params}
        assert "Idempotency-Key" in names, f"{path} must accept Idempotency-Key"
