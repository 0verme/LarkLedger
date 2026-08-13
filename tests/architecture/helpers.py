"""AST-based import graph helpers for architecture guard tests.

The guard formalises the v0.9.0 Platform / Channel-Neutral contract:

    Adapter (Feishu / Web / Client API / transport)
        → Application (ClientApplicationService)
            → Domain services
                → Core (models / schemas / context / repository)

Direction is enforced one way: inner layers must never import an outer layer
(no ``fastapi``, no Feishu client, no channel-specific route or token
transport). These helpers are intentionally dependency-free (stdlib only) so
the guard runs in the cheapest possible CI unit job.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "lark_ledger"


def module_of(path: Path) -> str:
    """Map a source file to its importable module name."""
    rel = path.relative_to(SRC)
    parts = list(rel.parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1][:-3]
    if not parts:
        return "lark_ledger"
    return "lark_ledger." + ".".join(parts)


def imported_modules(path: Path) -> set[str]:
    """Return every module name imported (directly) by ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def starts_with_any(module: str, prefixes: set[str]) -> bool:
    return any(module == prefix or module.startswith(prefix + ".") for prefix in prefixes)


def python_files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.rglob("*.py") if p.name != "__pycache__")


# ---------------------------------------------------------------------------
# Layer classification
# ---------------------------------------------------------------------------

#: Transport / channel adapters — may depend on anything but must never be
#: imported by an inner layer.
ADAPTER_MODULES = {
    "lark_ledger.main",
    "lark_ledger.api",
    "lark_ledger.web_api",
    "lark_ledger.client_api",
    "lark_ledger.dashboard_static",
    "lark_ledger.admin",
    "lark_ledger.services.feishu",
    "lark_ledger.services.feishu_client",
    "lark_ledger.services.feishu_crypto",
    "lark_ledger.services.message_processor",
    "lark_ledger.services.websocket",
    "lark_ledger.services.card_action",
    "lark_ledger.services.events",
    "lark_ledger.services.worker",
    "lark_ledger.services.reply_worker",
    "lark_ledger.services.recurring_worker",
    "lark_ledger.services.web_admin",
    "lark_ledger.services.dashboard_auth",
    "lark_ledger.services.client_auth",
    "lark_ledger.services.client_idempotency",
    "lark_ledger.readiness",
    "lark_ledger.system_status",
}

#: The single transport-neutral application boundary shared by every adapter.
APPLICATION_MODULES = {
    "lark_ledger.services.client_application",
    # P39: the Unified AI Entry is the channel-neutral AI command pipeline; it
    # orchestrates intent parsing + risk + the shared application boundary and
    # must therefore never be imported by a domain service.
    "lark_ledger.services.ai_entry",
}

#: Everything else under ``services/`` is a domain service.
_DOMAIN_COMMAND_PARSERS = frozenset(
    {
        "entry_commands",
        "account_commands",
        "goal_commands",
        "insight_commands",
        "household_commands",
        "ledger_commands",
        "recurring_commands",
        "transfer_commands",
    }
)


def is_domain_module(module: str) -> bool:
    if module.startswith("lark_ledger.services.") and (
        module not in ADAPTER_MODULES and module not in APPLICATION_MODULES
    ):
        return True
    tail = module.rsplit(".", 1)[-1]
    return tail in _DOMAIN_COMMAND_PARSERS


#: Inner core modules (models / schemas / context / commands / repository).
_CORE_EXCLUDED = {
    "main",
    "api",
    "web_api",
    "client_api",
    "admin",
    "dashboard_static",
    "readiness",
    "entry_commands",
    "account_commands",
    "goal_commands",
    "insight_commands",
    "household_commands",
    "ledger_commands",
    "recurring_commands",
    "transfer_commands",
}
CORE_FILES = [
    p
    for p in python_files(SRC)
    if p.parent == SRC and p.name != "__init__.py" and p.stem not in _CORE_EXCLUDED
]


def classify(path: Path) -> str:
    module = module_of(path)
    if module in ADAPTER_MODULES:
        return "adapter"
    if module in APPLICATION_MODULES:
        return "application"
    if is_domain_module(module):
        return "domain"
    return "core"
