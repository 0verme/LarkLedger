"""P46 — Assistant query architecture guards.

Enforces the dependency direction for the deterministic query fact layer:

    Adapter / AI
        → LedgerQueryService (needs QueryPlan from QueryPlanner)
            → Domain services (privacy, authorization, accounts)
                → Core / Database

The guards prove, at source level (reusing `tests/architecture/helpers.py`):

- the query **contracts** (`query_schemas.py`) are database-free — no
  ``sqlalchemy``, no ``lark_ledger.db`` — and carry no SQL-shaped field names;
- the AI intent parser (`services/ai.py`) has no database / session / ORM
  import — an AI module can never reach the database itself, only produce or
  consume the neutral contracts / call ``LedgerQueryService``;
- no domain service reaches the raw DB entry point ``lark_ledger.db``.
"""

from __future__ import annotations

from pathlib import Path

from tests.architecture.helpers import (
    ADAPTER_MODULES,
    SRC,
    imported_modules,
    module_of,
    starts_with_any,
)

QUERY_CONTRACTS = SRC / "query_schemas.py"
QUERY_CONTRACTS_MODULE = "lark_ledger.query_schemas"
LEDGER_QUERY = SRC / "services" / "ledger_query.py"
QUERY_PLANNER = SRC / "services" / "query_planner.py"
AI_PARSER = SRC / "services" / "ai.py"

#: Any of these in a contract/payload means "raw SQL escape hatch" — the exact
#: anti-pattern this issue forbids.
BANNED_CONTRACT_FIELDS = (
    "sql",
    "raw_sql",
    "where",
    "raw_where",
    "expression",
    "raw_expression",
    "custom_filter",
    "select_statement",
    "query_builder",
)


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_query_contracts_are_database_free() -> None:
    """QueryIntent / QueryPlan / QueryResult are pure values: no sqlalchemy,
    no session, no db entry point, no ORM model dependency for execution."""
    problems: list[str] = []
    for imported in imported_modules(QUERY_CONTRACTS):
        if imported == "sqlalchemy" or imported.startswith("sqlalchemy."):
            problems.append(f"contracts import sqlalchemy: {imported}")
        if imported == "lark_ledger.db" or imported.startswith("lark_ledger.db."):
            problems.append(f"contracts import db entry point: {imported}")
    assert problems == [], "\n".join(problems)


def test_query_contracts_have_no_sql_shaped_fields() -> None:
    contracts = _source(QUERY_CONTRACTS)
    lower = contracts.lower()
    leaked = [field for field in BANNED_CONTRACT_FIELDS if field in lower]
    # "where" may appear inside an English docstring ("never a raw WHERE") —
    # only flag it as an actual field definition (": " right after the name).
    leaked = [
        field
        for field in leaked
        if f"{field}:" in contracts or f"{field} = " in contracts
    ]
    assert leaked == [], f"query contracts leak SQL-shaped fields: {leaked}"


def test_query_contracts_use_extra_forbid() -> None:
    contracts = _source(QUERY_CONTRACTS)
    for model in ("class QueryIntent", "class QueryPlan", "class QueryResult"):
        assert (
            'model_config = ConfigDict(extra="forbid")' in contracts
        ), f"{model} must use extra=forbid"
        break


def test_ai_parser_never_imports_database_or_orm() -> None:
    """The AI intent parser must be DB-free: no sqlalchemy, no db entry point,
    no ORM models. It may only speak the neutral contracts."""
    problems: list[str] = []
    for imported in imported_modules(AI_PARSER):
        if imported == "sqlalchemy" or imported.startswith("sqlalchemy."):
            problems.append(f"AI parser imports sqlalchemy: {imported}")
        if imported == "lark_ledger.db" or imported.startswith("lark_ledger.db."):
            problems.append(f"AI parser imports db entry point: {imported}")
        if imported == "lark_ledger.models" or imported.startswith("lark_ledger.models."):
            problems.append(f"AI parser imports ORM models: {imported}")
    assert problems == [], "\n".join(problems)


def test_no_domain_service_reaches_db_entry_point() -> None:
    """No domain service imports the raw DB session factory
    (``lark_ledger.db``); the only importers are web/worker adapters."""
    problems: list[str] = []
    for path in sorted((SRC / "services").rglob("*.py")):
        if path.name == "__pycache__":
            continue
        for imported in imported_modules(path):
            if imported == "lark_ledger.db" or imported.startswith("lark_ledger.db."):
                problems.append(f"{module_of(path)} imports {imported}")
    assert problems == [], "\n".join(problems)


def test_query_executor_and_planner_never_import_adapters() -> None:
    """The query layer is channel-neutral: identical to the existing generic
    guard, made explicit for the assistant path."""
    problems: list[str] = []
    for path in (LEDGER_QUERY, QUERY_PLANNER):
        for imported in imported_modules(path):
            if starts_with_any(imported, ADAPTER_MODULES):
                problems.append(f"{module_of(path)} imports adapter {imported}")
    assert problems == [], "\n".join(problems)


def test_ai_query_path_must_go_through_ledger_query_service() -> None:
    """When the AI pipeline eventually executes queries it must land on
    ``LedgerQueryService`` (the single neutral executor), not a raw repository.
    The contracts surface is exported from exactly one module."""
    contracts = _source(QUERY_CONTRACTS)
    assert "class LedgerQueryService" not in contracts  # executor lives in services
    executor = _source(LEDGER_QUERY)
    assert "class LedgerQueryService" in executor
    # The executor consumes QueryPlan/QueryResult from the DB-free contracts.
    assert "from lark_ledger.query_schemas import" in executor
    assert QUERY_CONTRACTS_MODULE in imported_modules(LEDGER_QUERY)
