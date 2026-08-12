"""Architecture guard: core/application must never depend on an adapter.

Enforces the v0.9.0 dependency direction:

    Adapter (Feishu / Web / Client API / token transport)
        → Application (ClientApplicationService)
            → Domain
                → Core

An inner layer importing ``fastapi``, a Feishu client, a channel route, the
token transport or a worker would re-couple core business rules to one channel,
so the guard fails the build instead.
"""

from __future__ import annotations

from pathlib import Path

from tests.architecture.helpers import (
    ADAPTER_MODULES,
    APPLICATION_MODULES,
    CORE_FILES,
    SRC,
    classify,
    imported_modules,
    module_of,
    python_files,
    starts_with_any,
)

SERVICES_DIR = SRC / "services"

#: Third-party transport frameworks that are adapter-only concerns.
BANNED_THIRD_PARTY = {"fastapi", "starlette"}


def _all_inner_files() -> list[Path]:
    inner = list(CORE_FILES)
    inner += [p for p in python_files(SERVICES_DIR) if classify(p) in {"domain", "application"}]
    return sorted(inner)


def _violations(files: list[Path]) -> list[str]:
    problems: list[str] = []
    for path in files:
        module = module_of(path)
        for imported in imported_modules(path):
            if starts_with_any(imported, ADAPTER_MODULES | BANNED_THIRD_PARTY):
                problems.append(f"{module} imports adapter/transport module {imported}")
    return problems


def test_application_and_domain_never_import_fastapi_or_feishu() -> None:
    inner = [p for p in _all_inner_files() if classify(p) != "core"]
    violations = _violations(inner)
    assert violations == [], "\n".join(violations)


def test_core_never_imports_any_service_or_adapter() -> None:
    problems: list[str] = []
    for path in CORE_FILES:
        module = module_of(path)
        for imported in imported_modules(path):
            if imported.startswith("lark_ledger.services"):
                problems.append(f"{module} imports service layer {imported}")
            if starts_with_any(imported, ADAPTER_MODULES | BANNED_THIRD_PARTY):
                problems.append(f"{module} imports adapter/transport module {imported}")
    assert problems == [], "\n".join(problems)


def test_domain_never_imports_application_boundary() -> None:
    problems: list[str] = []
    for path in python_files(SERVICES_DIR):
        if classify(path) != "domain":
            continue
        module = module_of(path)
        for imported in imported_modules(path):
            if starts_with_any(imported, APPLICATION_MODULES):
                problems.append(f"{module} imports application boundary {imported}")
    assert problems == [], "\n".join(problems)


def test_request_context_is_platform_neutral() -> None:
    """RequestContext fields must be identity/scope, never channel secrets."""
    context_path = SRC / "context.py"
    tree = __import__("ast").parse(context_path.read_text(encoding="utf-8"))
    import ast

    dataclass_fields: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "RequestContext":
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    dataclass_fields.append(item.target.id)
    assert dataclass_fields, "RequestContext class not found"
    banned = {"open_id", "cookie", "token", "session", "api_key", "secret"}
    leaked = [f for f in dataclass_fields if any(b in f.lower() for b in banned)]
    assert leaked == [], f"RequestContext carries channel secrets: {leaked}"


def test_application_boundary_is_the_only_shared_application_layer() -> None:
    """Adapters must import the shared boundary, not reimplement it.

    A channel adapter may touch domain helpers (pending store, outbox) for
    channel-specific presentation, but the core write path must flow through
    ``ClientApplicationService`` (the single application layer).
    """
    boundary = SRC / "services" / "client_application.py"
    assert boundary.is_file()
    # The boundary itself must not depend on any adapter either.
    module = module_of(boundary)
    for imported in imported_modules(boundary):
        assert not starts_with_any(imported, ADAPTER_MODULES | BANNED_THIRD_PARTY), (
            f"{module} imports adapter/transport module {imported}"
        )


def test_application_boundary_authorizes_before_business() -> None:
    """Every financial write path must authorize first (no transport shortcut)."""
    boundary = (SRC / "services" / "client_application.py").read_text(encoding="utf-8")
    # The one shared financial command executor must start by resolving the
    # actor's access to the ledger before touching any business rule.
    assert "await self.authorize(context)" in boundary


def test_domain_error_model_has_no_http_exception() -> None:
    """Domain/application errors are transport-neutral, never HTTPException."""
    for path in [p for p in python_files(SERVICES_DIR) if classify(p) in {"domain", "application"}]:
        source = path.read_text(encoding="utf-8")
        assert "HTTPException" not in source, (
            f"{module_of(path)} leaks an HTTPException into the domain/application layer"
        )
    for path in CORE_FILES:
        source = path.read_text(encoding="utf-8")
        assert "HTTPException" not in source, (
            f"{module_of(path)} leaks an HTTPException into the core layer"
        )
