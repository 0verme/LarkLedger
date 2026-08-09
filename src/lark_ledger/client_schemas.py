from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from lark_ledger.models import Direction

ClientScope = Literal["ledger:read", "ledger:write", "pending:write"]


def _default_client_scopes() -> list[ClientScope]:
    return ["ledger:read", "ledger:write"]


class ClientErrorDetail(BaseModel):
    code: Literal[
        "authentication_required",
        "permission_denied",
        "resource_not_found",
        "validation_error",
        "conflict",
        "expired",
        "rate_limited",
        "temporary_failure",
    ]
    message: str
    request_id: str | None = None


class ClientErrorResponse(BaseModel):
    error: ClientErrorDetail


class ClientIdentity(BaseModel):
    user_id: str
    display_name: str
    ledger_id: str
    source_channel: str
    credential_id: str
    scopes: list[str]


class ClientLedger(BaseModel):
    id: str
    name: str
    kind: str
    currency: str
    timezone: str
    is_default: bool
    is_current: bool
    household_id: str | None


class ClientLedgerList(BaseModel):
    items: list[ClientLedger]


class ClientLedgerNameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)


class ClientEntryCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    direction: Direction
    category: str = Field(min_length=1, max_length=64)
    note: str = Field(default="", max_length=500)
    occurred_at: datetime
    currency: str | None = Field(default=None, min_length=3, max_length=3)


class ClientCommandResult(BaseModel):
    message: str
    resource: dict[str, Any] | None = None
    replayed: bool = False


class ClientCredentialCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    scopes: list[ClientScope] = Field(
        default_factory=_default_client_scopes, min_length=1, max_length=3
    )
    expires_at: datetime | None = None


class ClientCredentialView(BaseModel):
    id: str
    name: str
    token_prefix: str
    scopes: list[str]
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None


class ClientCredentialCreated(ClientCredentialView):
    token: str


class ClientCredentialList(BaseModel):
    items: list[ClientCredentialView]
