from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from lark_ledger.models import AccountStatus, AccountType, Direction

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


class ClientAccount(BaseModel):
    id: str
    ledger_id: str
    name: str
    type: AccountType
    subtype: str | None
    provider: str | None
    currency: str
    opening_balance: Decimal
    status: AccountStatus
    is_default: bool
    created_at: datetime
    updated_at: datetime


class ClientAccountList(BaseModel):
    items: list[ClientAccount]


class ClientAccountCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64)
    type: AccountType
    subtype: str | None = Field(default=None, max_length=32)
    provider: str | None = Field(default=None, max_length=64)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    opening_balance: Decimal = Field(default=Decimal("0"), max_digits=14, decimal_places=2)
    is_default: bool = False


class ClientAccountRenameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=64)


class ClientTransferCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_account_id: str
    to_account_id: str
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    occurred_at: datetime
    note: str = Field(default="", max_length=500)


class ClientTransfer(BaseModel):
    id: str
    ledger_id: str
    from_account_id: str
    to_account_id: str
    amount: Decimal
    currency: str
    note: str
    occurred_at: datetime
    reversed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ClientAccountBalance(BaseModel):
    account_id: str
    ledger_id: str
    account_name: str
    account_type: AccountType
    currency: str
    opening_balance: Decimal
    current_balance: Decimal
    archived: bool


class ClientAssetSummary(BaseModel):
    ledger_id: str
    currency: str
    total_assets: Decimal
    total_liabilities: Decimal
    net_assets: Decimal
    accounts: list[ClientAccountBalance]


class ClientEntryCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    direction: Direction
    category: str = Field(min_length=1, max_length=64)
    note: str = Field(default="", max_length=500)
    occurred_at: datetime
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    account_id: str | None = None


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
