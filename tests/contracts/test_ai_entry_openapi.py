"""P39 — OpenAPI contract for the Unified AI Entry endpoint.

Guards the ``POST /api/web/v1/ai/entries`` surface: the path exists, the
request/response schemas are fixed (never an arbitrary dict), and the schema
never leaks provider keys, the system prompt or session secrets.
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
            dashboard_session_secret="openapi-ai-test-secret-long-enough-123456",
            dashboard_cookie_secure=False,
            lark_app_id="cli_test",
            lark_app_secret="app-secret",
        )
    )
    return app.openapi()


def test_ai_entry_endpoint_is_published() -> None:
    schema = _schema()
    path = "/api/web/v1/ai/entries"
    assert path in schema["paths"], "AI entry endpoint missing from OpenAPI"
    assert "post" in schema["paths"][path]
    post = schema["paths"][path]["post"]
    assert post["operationId"].endswith("ai_entries_post")
    # json_text lowercases the payload.
    assert "idempotency-key" in json_text(post)
    assert "x-csrf-token" in json_text(post)


def test_ai_entry_request_schema_is_fixed() -> None:
    schema = _schema()
    components = schema["components"]["schemas"]
    request_name = "WebAIEntryRequest"
    assert request_name in components, "AI request schema must be named and fixed"
    request_schema = components[request_name]
    assert request_schema.get("type") == "object"
    props = request_schema["properties"]
    assert "text" in props
    assert "maxLength" in props["text"]
    # extra="forbid" — no free-form extension of the request.
    assert "additionalProperties" in request_schema
    assert not request_schema["additionalProperties"]


def test_ai_entry_response_schema_is_canonical() -> None:
    schema = _schema()
    components = schema["components"]["schemas"]
    assert "AIEntryResult" in components, "AI canonical result must be published"
    props = components["AIEntryResult"]["properties"]
    for required in (
        "status",
        "message",
        "request_id",
        "replayed",
        "operation",
        "pending_command_id",
        "confirmation_code",
        "expires_at",
        "missing_fields",
    ):
        assert required in props, f"AIEntryResult missing field {required}"
    status_ref = props["status"]["$ref"].rsplit("/", 1)[-1]
    status_schema = components[status_ref]
    assert "enum" in status_schema, "status must be an enum, not free text"
    assert {
        "executed",
        "confirmation_required",
        "clarification_required",
        "query_result",
        "rejected",
        "error",
    }.issubset(set(status_schema["enum"]))


def test_ai_entry_schema_leaks_no_secrets() -> None:
    schema_text = json_text(_schema())
    # Actual credential material must never appear: real key values, provider
    # endpoints, the system prompt, or session secrets. (Boolean config flags
    # such as ``ai_api_key_configured`` in the admin surface are fine — they
    # reveal presence, not the key itself.)
    for banned in (
        "sk-",  # real OpenAI/DeepSeek key prefixes
        "LARK_LEDGER_AI_API_KEY",
        "LARK_LEDGER_VISION_API_KEY",
        "LARK_LEDGER_TRANSCRIPTION_API_KEY",
        "system_prompt",
        "system prompt",
        "session_secret",
        "SESSION_SECRET",
        "openapi-ai-test-secret-long-enough-123456",
        "dashboard_session_secret",
    ):
        assert banned not in schema_text, f"OpenAPI leaks {banned}"


def json_text(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=True).lower()
