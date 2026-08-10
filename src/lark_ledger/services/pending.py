"""Pending high-risk command confirmation (P07).

When the risk router decides an image / voice / batch / likely-duplicate write
must not hit the ledger until the user confirms, the processor creates a
``pending_commands`` row holding the **frozen** ``ParsedCommand`` plus a frozen
user preview, and writes the preview card to the reply outbox in the same
transaction. The original event converges to ``succeeded``; the confirmation
itself is a later event (text ``确认 #C-A83F2`` or a card button action).

Confirming never re-calls AI or re-recognizes media: the frozen payload is
rehydrated with ``ParsedCommand.model_validate`` and executed exactly once under
a row lock.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, cast
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lark_ledger.config import Settings
from lark_ledger.confirmation_id import (
    format_confirmation_ref,
    generate_confirmation_code,
)
from lark_ledger.context import RequestContext
from lark_ledger.models import (
    Direction,
    LedgerEntry,
    PendingCommand,
    PendingStatus,
    ProcessedEvent,
    RecurringOccurrence,
    RecurringOccurrenceStatus,
    RecurringRule,
    ReplyOutbox,
)
from lark_ledger.outbox import (
    OUTBOX_PAYLOAD_VERSION,
    ReplyStatus,
    ReplyType,
    build_text_payload,
)
from lark_ledger.schemas import Action, ExecutionResult, ParsedCommand
from lark_ledger.services.accounts import AccountService
from lark_ledger.services.identity import IdentityService
from lark_ledger.services.ledger import LedgerService
from lark_ledger.services.ledger_authorization import LedgerAuthorizationService
from lark_ledger.services.risk import RiskAssessment, RiskDecision, RiskReason
from lark_ledger.services.transfers import TransferService
from lark_ledger.services.worker import is_permanent_error
from lark_ledger.short_id import MAX_SHORT_ID_ALLOCATION_ATTEMPTS, normalize_entry_ref

logger = logging.getLogger(__name__)

#: Truncation bound for per-entry note shown in the preview.
NOTE_PREVIEW_LEN = 20
#: Card action marker shared with the card action handler (P07).
CARD_ACTION_KEY = "larkledger_pending"

_REASON_TEXT = {
    RiskReason.TRANSFER: "账户转账",
    RiskReason.VISION: "图片识别",
    RiskReason.TRANSCRIPTION: "语音识别",
    RiskReason.BATCH: "批量记账",
    RiskReason.CREATE_ENTRIES: "批量记账",
    RiskReason.BUDGETS: "批量预算",
    RiskReason.DUPLICATE: "疑似重复",
    RiskReason.RECURRING: "周期账单",
}

#: Deterministic source key for recurring-generated pendings.
RECURRING_SOURCE_PREFIX = "recurring:"


@dataclass(frozen=True)
class PendingPreviewItem:
    index: int | None
    direction: str
    amount: str
    currency: str
    category: str
    occurred_at: str
    note: str
    duplicate_of: str | None = None


@dataclass(frozen=True)
class PendingPreview:
    """Frozen, user-facing preview of a pending command (no raw media text)."""

    code: str = ""  # storage form, e.g. CA83F2
    display_code: str = ""  # #C-A83F2
    entries_total: int = 0
    income_count: int = 0
    expense_count: int = 0
    income_total: str = ""
    expense_total: str = ""
    currency: str = ""
    items: list[PendingPreviewItem] = field(default_factory=list)
    budgets: list[dict[str, Any]] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)
    risk_reason: str = ""
    expires_at: str = ""

    def as_json(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "display_code": self.display_code,
            "entries_total": self.entries_total,
            "income_count": self.income_count,
            "expense_count": self.expense_count,
            "income_total": self.income_total,
            "expense_total": self.expense_total,
            "currency": self.currency,
            "items": [item.__dict__ for item in self.items],
            "budgets": self.budgets,
            "anomalies": self.anomalies,
            "risk_reason": self.risk_reason,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> PendingPreview:
        items = [PendingPreviewItem(**item) for item in data.get("items", [])]
        return cls(**{**data, "items": items})


def _as_utc(value: datetime) -> datetime:
    """SQLite reads ``DateTime(timezone=True)`` back as naive; treat as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _format_local_datetime(value: datetime | str, timezone: str, format_string: str) -> str:
    """Format a stored UTC timestamp in the configured application timezone."""
    if not value:
        return ""
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    return _as_utc(parsed).astimezone(ZoneInfo(timezone)).strftime(format_string)


def _direction_label(value: Direction | str | None) -> str:
    if isinstance(value, Direction):
        return value.value
    return str(value or "")


def _format_amount(amount: Decimal | str | None) -> str:
    if amount is None:
        return ""
    return f"{Decimal(amount):.2f}"


def _format_occurred(occurred_at: datetime | str | None) -> str:
    if occurred_at is None:
        return ""
    if isinstance(occurred_at, str):
        occurred_at = datetime.fromisoformat(occurred_at)
    return occurred_at.strftime("%Y-%m-%d %H:%M")


def build_pending_preview(
    command: ParsedCommand,
    source_type: str,
    risk: RiskAssessment,
    *,
    now: datetime,
    expires_seconds: int,
    currency: str,
) -> PendingPreview:
    """Aggregate a frozen preview from structured command fields only.

    Never includes OCR text, transcripts, or raw media content: only amounts,
    directions, categories, times, and truncated notes already validated by the
    schema. The confirmation code is allocated by the caller afterwards.
    """
    duplicate_by_index: dict[int | None, str] = {
        hit.entry_index: hit.existing_short_id for hit in risk.duplicate_hits
    }
    items: list[PendingPreviewItem] = []
    if command.action is Action.CREATE:
        items.append(
            PendingPreviewItem(
                index=None,
                direction=_direction_label(command.direction),
                amount=_format_amount(command.amount),
                currency=command.currency or currency,
                category=command.category or "",
                occurred_at=_format_occurred(command.occurred_at),
                note=(command.note or "")[:NOTE_PREVIEW_LEN],
                duplicate_of=duplicate_by_index.get(None),
            )
        )
    elif command.action is Action.TRANSFER:
        items.append(
            PendingPreviewItem(
                index=None,
                direction="transfer",
                amount=_format_amount(command.amount),
                currency=command.currency or currency,
                category=f"{command.from_account_hint} → {command.to_account_hint}",
                occurred_at=_format_occurred(command.occurred_at),
                note=(command.note or "")[:NOTE_PREVIEW_LEN],
            )
        )
    elif command.entries:
        for index, candidate in enumerate(command.entries):
            items.append(
                PendingPreviewItem(
                    index=index,
                    direction=_direction_label(candidate.direction),
                    amount=_format_amount(candidate.amount),
                    currency=candidate.currency or currency,
                    category=candidate.category or "",
                    occurred_at=_format_occurred(candidate.occurred_at),
                    note=(candidate.note or "")[:NOTE_PREVIEW_LEN],
                    duplicate_of=duplicate_by_index.get(index),
                )
            )

    budgets: list[dict[str, Any]] = []
    if command.action is Action.SET_BUDGET:
        budgets = [
            {
                "category": command.category or "",
                "amount": _format_amount(command.amount),
                "currency": command.currency or currency,
            }
        ]
    elif command.budgets:
        budgets = [
            {
                "category": candidate.category or "",
                "amount": _format_amount(candidate.amount),
                "currency": candidate.currency or currency,
            }
            for candidate in command.budgets
        ]

    income_count = sum(1 for item in items if item.direction == "income")
    expense_count = sum(1 for item in items if item.direction == "expense")
    income_total = sum(
        (Decimal(item.amount) for item in items if item.direction == "income"),
        Decimal("0"),
    )
    expense_total = sum(
        (Decimal(item.amount) for item in items if item.direction == "expense"),
        Decimal("0"),
    )

    anomalies: list[str] = []
    if command.batch_truncated:
        anomalies.append("批量结果超出上限，仅保留部分条目")
    if command.budgets_truncated:
        anomalies.append("预算超出上限，仅保留部分预算项")
    missing_categories = sum(1 for item in items if not item.category)
    if missing_categories:
        anomalies.append(f"有 {missing_categories} 笔缺少分类")
    for hit in risk.duplicate_hits:
        anomalies.append(f"疑似与账目 #{hit.existing_short_id} 重复")

    expires = now + timedelta(seconds=expires_seconds)
    return PendingPreview(
        entries_total=len(items),
        income_count=income_count,
        expense_count=expense_count,
        income_total=f"{income_total:.2f}",
        expense_total=f"{expense_total:.2f}",
        currency=currency,
        items=items,
        budgets=budgets,
        anomalies=anomalies,
        risk_reason=(
            _REASON_TEXT.get(risk.reason, "高风险写入") if risk.reason is not None else "高风险写入"
        ),
        expires_at=expires.isoformat(),
    )


def build_pending_preview_card(preview: PendingPreview, *, timezone: str) -> dict[str, Any]:
    """Render a confirmation preview card (schema 2.0) with 确认 / 取消 buttons.

    Each button uses the JSON 2.0 callback behavior to carry the storage
    confirmation code.  The callback handler re-verifies operator user +
    pending status, and text commands remain the fallback path.
    """
    elements: list[dict[str, Any]] = []
    header_lines = [
        f"**确认单 {preview.display_code}**",
        f"原因：{preview.risk_reason}",
        f"过期时间：{_format_local_datetime(preview.expires_at, timezone, '%Y-%m-%d %H:%M')}",
    ]
    if preview.entries_total:
        header_lines.append(
            f"共 {preview.entries_total} 笔 · 支出 {preview.expense_total} · "
            f"收入 {preview.income_total}（{preview.currency}）"
        )
    elements.append({"tag": "markdown", "content": "\n".join(header_lines)})

    for index, item in enumerate(preview.items):
        direction = "支出" if item.direction == "expense" else "收入"
        dup = f"  ⚠️疑似重复 {item.duplicate_of}" if item.duplicate_of else ""
        content = (
            f"{index + 1}. {direction} {item.amount} {item.currency} "
            f"· {item.category or '未分类'} · {item.occurred_at}"
            f"{(' · ' + item.note) if item.note else ''}{dup}"
        )
        elements.append({"tag": "markdown", "content": content})

    for budget in preview.budgets:
        elements.append(
            {
                "tag": "markdown",
                "content": (
                    f"📌 预算 {budget['category']} {budget['amount']} {budget['currency']}"
                ),
            }
        )

    for anomaly in preview.anomalies:
        elements.append({"tag": "markdown", "content": f"⚠️ {anomaly}"})

    elements.append(
        {
            "tag": "markdown",
            "content": (
                f"回复 `确认 {preview.display_code}` 确认，或 `取消 {preview.display_code}` 取消。"
            ),
        }
    )
    code_suffix = preview.code[1:] if preview.code else ""
    elements.extend(
        [
            {
                "tag": "button",
                "element_id": "confirm_pending",
                "text": {"tag": "plain_text", "content": "确认"},
                "type": "primary",
                "width": "fill",
                "behaviors": [
                    {
                        "type": "callback",
                        "value": {
                            "k": CARD_ACTION_KEY,
                            "action": "confirm",
                            "code": code_suffix,
                        },
                    }
                ],
            },
            {
                "tag": "button",
                "element_id": "cancel_pending",
                "text": {"tag": "plain_text", "content": "取消"},
                "type": "danger",
                "width": "fill",
                "behaviors": [
                    {
                        "type": "callback",
                        "value": {
                            "k": CARD_ACTION_KEY,
                            "action": "cancel",
                            "code": code_suffix,
                        },
                    }
                ],
            },
        ]
    )
    return {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": "待确认记账"},
            "subtitle": {"tag": "plain_text", "content": preview.display_code},
            "template": "blue",
        },
        "body": {
            "direction": "vertical",
            "padding": "12px 12px 12px 12px",
            "elements": elements,
        },
    }


class PendingCommandStore:
    """Create / confirm / cancel / query pending commands."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self._factory = session_factory
        self._settings = settings

    async def create_pending(
        self,
        *,
        session: AsyncSession,
        event_id: str | None,
        message_id: str,
        source_fingerprint: str | None,
        user_open_id: str,
        command: ParsedCommand,
        source_type: str,
        risk: RiskAssessment,
        now: datetime,
        context: RequestContext | None = None,
    ) -> PendingCommand:
        """Add a pending row to ``session`` (the caller commits it together with
        the preview outbox). Allocates a user-unique confirmation code via an
        in-session pre-check plus the unique constraint as the concurrency guard.
        """
        if context is None:
            context = await IdentityService(
                session,
                currency=self._settings.currency,
                timezone=self._settings.timezone,
            ).resolve_or_bootstrap(
                channel="feishu",
                external_subject_id=user_open_id,
            )
        from_account_id: uuid.UUID | None = None
        to_account_id: uuid.UUID | None = None
        transfer_id: uuid.UUID | None = None
        account_id: uuid.UUID | None = None
        if command.action is Action.TRANSFER:
            assert command.from_account_hint is not None
            assert command.to_account_hint is not None
            resolver = TransferService(session)
            from_account_id = (
                await resolver.resolve_account_hint(context, command.from_account_hint)
            ).id
            to_account_id = (
                await resolver.resolve_account_hint(context, command.to_account_hint)
            ).id
            if from_account_id == to_account_id:
                raise ValueError("转出和转入账户不能相同")
            transfer_id = uuid.uuid4()
        elif command.action in {
            Action.CREATE,
            Action.UPDATE_LAST,
            Action.CREATE_ENTRIES,
            Action.BATCH,
        }:
            # Freeze the single account the frozen command targets. Resolve the
            # hint at creation time so confirming after the user switches ledger
            # or changes the default account still writes to the frozen target.
            account_id = (
                await TransferService(session).resolve_account_hint(
                    context, command.account_hint
                )
            ).id if command.account_hint is not None else (
                await AccountService(session).get_default(context)
            ).id
        elif (
            command.action in {Action.UPDATE_ENTRY, Action.DELETE_ENTRY, Action.RESTORE_ENTRY}
            and command.entry_ref
        ):
            # Freeze the entry's target account. An explicit hint names the new
            # account; otherwise the entry's current account is frozen so an
            # intervening default-account change never redirects the mutation.
            if command.account_hint is not None:
                account_id = (
                    await TransferService(session).resolve_account_hint(
                        context, command.account_hint
                    )
                ).id
            else:
                try:
                    code = normalize_entry_ref(command.entry_ref)
                except Exception:
                    code = command.entry_ref
                account_id = await session.scalar(
                    select(LedgerEntry.account_id).where(
                        LedgerEntry.ledger_id == context.ledger_id,
                        LedgerEntry.short_id == code,
                    )
                )
        preview = build_pending_preview(
            command,
            source_type,
            risk,
            now=now,
            expires_seconds=self._settings.pending_expires_seconds,
            currency=self._settings.currency,
        )
        code = await self._allocate_code(session, user_open_id)
        preview = replace(preview, code=code, display_code=format_confirmation_ref(code))
        pending = PendingCommand(
            confirmation_code=code,
            user_open_id=user_open_id,
            actor_user_id=context.actor_user_id,
            ledger_id=context.ledger_id,
            from_account_id=from_account_id,
            to_account_id=to_account_id,
            transfer_id=transfer_id,
            account_id=account_id,
            source_event_id=event_id,
            source_message_id=message_id,
            source_fingerprint=source_fingerprint,
            transport="feishu",
            source_type=source_type,
            command_type=command.action.value,
            payload_version=1,
            payload_json=command.model_dump(mode="json"),
            preview_json=preview.as_json(),
            risk_reason=risk.reason.value if risk.reason else "high_risk",
            status=PendingStatus.PENDING.value,
            expires_at=now + timedelta(seconds=self._settings.pending_expires_seconds),
        )
        session.add(pending)
        return pending

    async def create_recurring_pending(
        self,
        *,
        session: AsyncSession,
        context: RequestContext,
        user_open_id: str,
        rule: RecurringRule,
        occurrence_date: date,
        now: datetime,
    ) -> PendingCommand:
        """Add a pending row for one recurring occurrence to ``session``.

        The pending freezes the rule's ledger, account, amount, currency,
        category, planned business date and the occurrence identity
        (``source_event_id = recurring:{rule_id}:{occurrence_date}``), so the
        user can switch ledger / default account / rule later and confirming
        still writes to the frozen target. The caller commits the pending
        together with the occurrence row and the reminder outbox.
        """
        scheduled_at = datetime.combine(
            occurrence_date, time.min, tzinfo=ZoneInfo(self._settings.timezone)
        ).astimezone(UTC)
        command = ParsedCommand(
            action=Action.CREATE,
            amount=rule.amount,
            currency=rule.currency if rule.currency != self._settings.currency else None,
            direction=rule.transaction_type,
            category=rule.category,
            note=rule.description or None,
            occurred_at=scheduled_at,
        )
        occurrence_key = f"{RECURRING_SOURCE_PREFIX}{rule.id}:{occurrence_date.isoformat()}"
        code = await self._allocate_code(session, user_open_id)
        risk = RiskAssessment(
            decision=RiskDecision.PENDING,
            reason=RiskReason.RECURRING,
        )
        preview = build_pending_preview(
            command,
            "recurring",
            risk,
            now=now,
            expires_seconds=self._settings.pending_expires_seconds,
            currency=self._settings.currency,
        )
        preview = replace(preview, code=code, display_code=format_confirmation_ref(code))
        pending = PendingCommand(
            confirmation_code=code,
            user_open_id=user_open_id,
            actor_user_id=context.actor_user_id,
            ledger_id=context.ledger_id,
            account_id=rule.account_id,
            recurring_rule_id=rule.id,
            occurrence_date=occurrence_date,
            source_event_id=occurrence_key,
            source_message_id=occurrence_key,
            source_fingerprint=None,
            transport="recurring",
            source_type="recurring",
            command_type=Action.CREATE.value,
            payload_version=1,
            payload_json=command.model_dump(mode="json"),
            preview_json=preview.as_json(),
            risk_reason=RiskReason.RECURRING.value,
            status=PendingStatus.PENDING.value,
            expires_at=now + timedelta(seconds=self._settings.pending_expires_seconds),
        )
        session.add(pending)
        await session.flush()
        return pending

    async def has_active_fingerprint(self, ledger_id: uuid.UUID, source_fingerprint: str) -> bool:
        """Return whether this ledger's exact media already awaits a decision."""
        async with self._factory() as session:
            pending_id = await session.scalar(
                select(PendingCommand.id)
                .where(
                    PendingCommand.ledger_id == ledger_id,
                    PendingCommand.source_fingerprint == source_fingerprint,
                    PendingCommand.status.in_(
                        [
                            PendingStatus.PENDING.value,
                            PendingStatus.EXECUTING.value,
                        ]
                    ),
                )
                .limit(1)
            )
            return pending_id is not None

    async def _allocate_code(self, session: AsyncSession, user_open_id: str) -> str:
        for _ in range(MAX_SHORT_ID_ALLOCATION_ATTEMPTS):
            candidate = generate_confirmation_code()
            collision = any(
                isinstance(obj, PendingCommand)
                and obj.user_open_id == user_open_id
                and obj.confirmation_code == candidate
                for obj in session.new
            )
            if collision:
                continue
            existing = await session.scalar(
                select(PendingCommand.id)
                .where(
                    PendingCommand.user_open_id == user_open_id,
                    PendingCommand.confirmation_code == candidate,
                )
                .limit(1)
            )
            if existing is None:
                return candidate
        raise RuntimeError(
            f"failed to allocate confirmation code after "
            f"{MAX_SHORT_ID_ALLOCATION_ATTEMPTS} attempts"
        )

    async def get_by_code(self, user_open_id: str, confirmation_code: str) -> PendingCommand | None:
        async with self._factory() as session:
            return cast(
                PendingCommand | None,
                await session.scalar(
                    select(PendingCommand).where(
                        PendingCommand.user_open_id == user_open_id,
                        PendingCommand.confirmation_code == confirmation_code,
                    )
                ),
            )

    async def list_for_user(
        self, user_open_id: str, *, limit: int | None = None
    ) -> list[PendingCommand]:
        async with self._factory() as session:
            stmt = (
                select(PendingCommand)
                .where(PendingCommand.user_open_id == user_open_id)
                .order_by(PendingCommand.created_at.desc())
            )
            if limit is not None:
                stmt = stmt.limit(limit)
            return list((await session.execute(stmt)).scalars().all())

    async def list_all(self, *, limit: int = 50) -> list[PendingCommand]:
        """Cross-user pending listing for the operator CLI (aggregates only)."""
        async with self._factory() as session:
            return list(
                (
                    await session.execute(
                        select(PendingCommand)
                        .order_by(PendingCommand.created_at.desc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )

    def _make_text_row(self, *, message_id: str, event_id: str | None, text: str) -> ReplyOutbox:
        return ReplyOutbox(
            event_id=event_id,
            message_id=message_id,
            reply_type=ReplyType.TEXT.value,
            sequence=0,
            transport="feishu",
            payload_version=OUTBOX_PAYLOAD_VERSION,
            payload_json=build_text_payload(text),
            payload_blob=None,
            status=ReplyStatus.PENDING.value,
            attempt_count=0,
        )

    @staticmethod
    def _terminal_message(status: str) -> str:
        if status == PendingStatus.EXECUTED.value:
            return "该笔已确认并已入账，无需重复操作。"
        if status == PendingStatus.CANCELLED.value:
            return "该确认单已取消。"
        if status == PendingStatus.EXPIRED.value:
            return "该确认单已过期。"
        if status == PendingStatus.FAILED.value:
            return "该确认单处理失败，请重新发送原记账内容。"
        return "该确认单当前状态不可操作。"

    async def confirm_and_execute(
        self,
        *,
        user_open_id: str,
        confirmation_code: str,
        reply_to_message_id: str,
        confirm_event_id: str | None,
        exchange_rates: Any,
        now: datetime,
    ) -> tuple[str, list[ReplyOutbox]]:
        """Execute the frozen command exactly once under a row lock.

        The result reply outbox row is committed in the same transaction as the
        ``executed`` state, so a crashed re-delivery converges via the outbox /
        ``business_committed_at`` pre-checks without re-running business.
        """
        async with self._factory() as session:
            # Lock the frozen transport identity first. Legacy pending rows may
            # predate internal User/Ledger records; serializing here prevents
            # concurrent confirms from racing identity/account bootstrap.
            row = await session.scalar(
                select(PendingCommand)
                .where(
                    PendingCommand.user_open_id == user_open_id,
                    PendingCommand.confirmation_code == confirmation_code,
                )
                .with_for_update()
            )
            if row is None:
                message = "未找到该确认单，或它不属于当前用户。"
                reply = self._make_text_row(
                    message_id=reply_to_message_id, event_id=confirm_event_id, text=message
                )
                session.add(reply)
                await session.commit()
                return message, [reply]
            actor_context = await IdentityService(
                session,
                currency=self._settings.currency,
                timezone=self._settings.timezone,
            ).resolve_or_bootstrap(
                channel="feishu",
                external_subject_id=user_open_id,
            )
            legacy_unscoped = row.actor_user_id is None and row.ledger_id is None
            if not legacy_unscoped and (
                row.actor_user_id != actor_context.actor_user_id or row.ledger_id is None
            ):
                message = "该确认单所属账本已不可访问，未执行任何写入。"
                reply = self._make_text_row(
                    message_id=reply_to_message_id, event_id=confirm_event_id, text=message
                )
                session.add(reply)
                await session.commit()
                return message, [reply]
            frozen_ledger_id = row.ledger_id or actor_context.ledger_id
            if not await LedgerAuthorizationService(session).can_access(
                actor_context.actor_user_id, frozen_ledger_id
            ):
                message = "该确认单所属账本已不可访问，未执行任何写入。"
                reply = self._make_text_row(
                    message_id=reply_to_message_id, event_id=confirm_event_id, text=message
                )
                session.add(reply)
                await session.commit()
                return message, [reply]
            if row.status != PendingStatus.PENDING.value:
                message = self._terminal_message(row.status)
                reply = self._make_text_row(
                    message_id=reply_to_message_id, event_id=confirm_event_id, text=message
                )
                session.add(reply)
                await session.commit()
                return message, [reply]
            if row.expires_at is not None and _as_utc(row.expires_at) <= now:
                row.status = PendingStatus.EXPIRED.value
                message = "该确认单已过期，请重新发送原记账内容。"
                reply = self._make_text_row(
                    message_id=reply_to_message_id, event_id=confirm_event_id, text=message
                )
                session.add(reply)
                await session.commit()
                return message, [reply]

            pending_id = row.id
            row.status = PendingStatus.EXECUTING.value
            command = ParsedCommand.model_validate(row.payload_json)
            try:
                frozen_context = RequestContext(
                    actor_user_id=actor_context.actor_user_id,
                    ledger_id=frozen_ledger_id,
                    source_channel=row.transport,
                    external_subject_id=user_open_id,
                )
                if command.action is Action.TRANSFER:
                    if (
                        row.from_account_id is None
                        or row.to_account_id is None
                        or row.transfer_id is None
                        or command.amount is None
                        or command.occurred_at is None
                    ):
                        raise ValueError("pending transfer target is incomplete")
                    transfer = await TransferService(session).create(
                        frozen_context,
                        from_account_id=row.from_account_id,
                        to_account_id=row.to_account_id,
                        amount=command.amount,
                        occurred_at=command.occurred_at,
                        note=command.note or "",
                        source_type=row.source_type,
                        source_message_id=row.source_message_id,
                        transfer_id=row.transfer_id,
                    )
                    result = ExecutionResult(
                        message=f"转账已创建：{transfer.amount} {transfer.currency}"
                    )
                else:
                    result = await LedgerService(
                        session,
                        self._settings.currency,
                        self._settings.timezone,
                        exchange_rates=exchange_rates,
                        commit_changes=False,
                        account_id=row.account_id,
                    ).execute(
                        frozen_context,
                        command,
                        source_type=row.source_type,
                        source_message_id=row.source_message_id,
                    )
            except Exception as exc:
                # Roll back the EXECUTING write. Deterministic failures mark the
                # pending failed (a retried confirm then gets the idempotent
                # message instead of re-running business); transient failures
                # propagate so the event worker retries the event.
                await session.rollback()
                if not is_permanent_error(exc):
                    raise
                await session.execute(
                    update(PendingCommand)
                    .where(
                        PendingCommand.id == pending_id,
                        PendingCommand.status.in_(
                            [
                                PendingStatus.PENDING.value,
                                PendingStatus.EXECUTING.value,
                            ]
                        ),
                    )
                    .values(status=PendingStatus.FAILED.value, updated_at=now)
                )
                await self._mark_occurrence_failed(session, row, now)
                message = "该确认单处理失败，请重新发送原记账内容。"
                reply = self._make_text_row(
                    message_id=reply_to_message_id, event_id=confirm_event_id, text=message
                )
                session.add(reply)
                await session.commit()
                logger.info(
                    "pending confirmation failed confirmation_code=%s error_code=%s",
                    confirmation_code,
                    type(exc).__name__,
                )
                return message, [reply]

            row.status = PendingStatus.EXECUTED.value
            row.confirmed_at = now
            row.executed_at = now
            await self._mark_occurrence_confirmed(session, row, result.entry_id)
            reply = self._make_text_row(
                message_id=reply_to_message_id,
                event_id=confirm_event_id,
                text=result.message,
            )
            session.add(reply)
            if confirm_event_id is not None:
                parent = await session.get(ProcessedEvent, confirm_event_id)
                if parent is not None:
                    parent.business_committed_at = now
            await session.commit()
            logger.info(
                "pending confirmation executed confirmation_code=%s event_id=%s",
                confirmation_code,
                confirm_event_id,
            )
            return result.message, [reply]

    @staticmethod
    async def _mark_occurrence_confirmed(
        session: AsyncSession,
        pending: PendingCommand,
        entry_id: uuid.UUID | None,
    ) -> None:
        """Link a confirmed recurring pending to its occurrence (P29).

        Only touches the occurrence that this pending names; a non-recurring
        pending is a no-op. The row lock on the pending already serializes
        duplicate confirms, so exactly one occurrence transitions to confirmed.
        """
        if pending.recurring_rule_id is None or pending.occurrence_date is None:
            return
        occurrence = await session.scalar(
            select(RecurringOccurrence).where(
                RecurringOccurrence.rule_id == pending.recurring_rule_id,
                RecurringOccurrence.occurrence_date == pending.occurrence_date,
            )
        )
        if occurrence is not None and occurrence.status == RecurringOccurrenceStatus.PENDING.value:
            occurrence.status = RecurringOccurrenceStatus.CONFIRMED.value
            occurrence.entry_id = entry_id

    @staticmethod
    async def _mark_occurrence_failed(
        session: AsyncSession,
        pending: PendingCommand,
        now: datetime,
    ) -> None:
        """Terminate the occurrence of a permanently-failed recurring confirm."""
        if pending.recurring_rule_id is None or pending.occurrence_date is None:
            return
        occurrence = await session.scalar(
            select(RecurringOccurrence).where(
                RecurringOccurrence.rule_id == pending.recurring_rule_id,
                RecurringOccurrence.occurrence_date == pending.occurrence_date,
            )
        )
        if occurrence is not None and occurrence.status == RecurringOccurrenceStatus.PENDING.value:
            occurrence.status = RecurringOccurrenceStatus.FAILED.value

    async def cancel(
        self,
        *,
        user_open_id: str,
        confirmation_code: str,
        reply_to_message_id: str,
        cancel_event_id: str | None,
        now: datetime,
    ) -> tuple[str, list[ReplyOutbox]]:
        """Cancel a pending confirmation (idempotent, user-scoped)."""
        async with self._factory() as session:
            row = await session.scalar(
                select(PendingCommand)
                .where(
                    PendingCommand.user_open_id == user_open_id,
                    PendingCommand.confirmation_code == confirmation_code,
                )
                .with_for_update()
            )
            if row is None:
                message = "未找到该确认单，或它不属于当前用户。"
                reply = self._make_text_row(
                    message_id=reply_to_message_id, event_id=cancel_event_id, text=message
                )
                session.add(reply)
                await session.commit()
                return message, [reply]
            if row.status != PendingStatus.PENDING.value:
                message = self._terminal_message(row.status)
                reply = self._make_text_row(
                    message_id=reply_to_message_id, event_id=cancel_event_id, text=message
                )
                session.add(reply)
                await session.commit()
                return message, [reply]
            if row.expires_at is not None and _as_utc(row.expires_at) <= now:
                row.status = PendingStatus.EXPIRED.value
                message = "该确认单已过期，请重新发送原记账内容。"
                reply = self._make_text_row(
                    message_id=reply_to_message_id, event_id=cancel_event_id, text=message
                )
                session.add(reply)
                await session.commit()
                return message, [reply]

            row.status = PendingStatus.CANCELLED.value
            row.cancelled_at = now
            await self._mark_occurrence_cancelled(session, row)
            message = f"已取消 {format_confirmation_ref(confirmation_code)}，未写入账本。"
            reply = self._make_text_row(
                message_id=reply_to_message_id, event_id=cancel_event_id, text=message
            )
            session.add(reply)
            if cancel_event_id is not None:
                parent = await session.get(ProcessedEvent, cancel_event_id)
                if parent is not None:
                    parent.business_committed_at = now
            await session.commit()
            return message, [reply]

    @staticmethod
    async def _mark_occurrence_cancelled(
        session: AsyncSession, pending: PendingCommand
    ) -> None:
        """Terminate the occurrence of a cancelled recurring pending (P29)."""
        if pending.recurring_rule_id is None or pending.occurrence_date is None:
            return
        occurrence = await session.scalar(
            select(RecurringOccurrence).where(
                RecurringOccurrence.rule_id == pending.recurring_rule_id,
                RecurringOccurrence.occurrence_date == pending.occurrence_date,
            )
        )
        if occurrence is not None and occurrence.status == RecurringOccurrenceStatus.PENDING.value:
            occurrence.status = RecurringOccurrenceStatus.CANCELLED.value

    async def list_pending(
        self,
        *,
        user_open_id: str,
        reply_to_message_id: str,
        event_id: str | None,
        limit: int | None = None,
    ) -> tuple[str, list[ReplyOutbox]]:
        """Render the user's recent pending confirmations as a text reply."""
        count = limit or self._settings.pending_max_list
        async with self._factory() as session:
            context = await IdentityService(
                session,
                currency=self._settings.currency,
                timezone=self._settings.timezone,
            ).resolve_or_bootstrap(
                channel="feishu",
                external_subject_id=user_open_id,
            )
            rows = (
                (
                    await session.execute(
                        select(PendingCommand)
                        .where(
                            PendingCommand.user_open_id == user_open_id,
                            or_(
                                PendingCommand.ledger_id == context.ledger_id,
                                PendingCommand.ledger_id.is_(None),
                            ),
                        )
                        .order_by(PendingCommand.created_at.desc())
                        .limit(count)
                    )
                )
                .scalars()
                .all()
            )
            if not rows:
                message = "当前没有待确认的记账。"
                reply = self._make_text_row(
                    message_id=reply_to_message_id, event_id=event_id, text=message
                )
                session.add(reply)
                await session.commit()
                return message, [reply]
            lines: list[str] = []
            for pending in rows:
                preview = PendingPreview.from_json(pending.preview_json)
                expires = (
                    _format_local_datetime(
                        pending.expires_at,
                        self._settings.timezone,
                        "%m-%d %H:%M",
                    )
                    if pending.expires_at
                    else "未知"
                )
                lines.append(
                    f"{format_confirmation_ref(pending.confirmation_code)} · "
                    f"{preview.risk_reason} · 共 {preview.entries_total} 笔"
                    f"（过期 {expires}）"
                )
            message = "待确认列表：\n" + "\n".join(lines)
            reply = self._make_text_row(
                message_id=reply_to_message_id, event_id=event_id, text=message
            )
            session.add(reply)
            await session.commit()
            return message, [reply]
