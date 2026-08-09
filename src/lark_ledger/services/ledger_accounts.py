"""Feishu chat read queries for accounts and asset summary (P26/P27).

Resolves ledger-scoped account hints server-side and never trusts an
``account_id`` from chat input: a hint is normalized and must match exactly one
active account in the current ledger.
"""
# mypy: disable-error-code="attr-defined"

from __future__ import annotations

from lark_ledger.models import AccountType
from lark_ledger.schemas import ExecutionResult, ParsedCommand
from lark_ledger.services.accounts import AccountService
from lark_ledger.services.transfers import AccountBalance, TransferService


class _AccountQueryMixin:
    """Feishu account list / single balance / asset summary replies."""

    async def _list_accounts(self, user_open_id: str, command: ParsedCommand) -> ExecutionResult:
        context = self._request_context()
        transfer_service = TransferService(self.session)
        if command.account_hint is not None:
            account = await transfer_service.resolve_account_hint(context, command.account_hint)
            balance = await transfer_service.account_balance(context, account.id)
            return ExecutionResult(message=self._account_balance_line(balance))
        accounts = await AccountService(self.session).list(context, include_archived=True)
        if not accounts:
            return ExecutionResult(message="当前账本还没有账户。")
        lines = []
        for account in accounts:
            balance = await transfer_service.account_balance(context, account.id)
            lines.append(self._account_balance_line(balance))
        return ExecutionResult(message="账户列表：\n" + "\n".join(lines))

    async def _assets(self, user_open_id: str, command: ParsedCommand) -> ExecutionResult:
        summary = await TransferService(self.session).asset_summary(self._request_context())
        return ExecutionResult(
            message=(
                f"总资产：{self._format_money(summary.total_assets)}\n"
                f"总负债：{self._format_money(summary.total_liabilities)}\n"
                f"净资产：{self._format_money(summary.net_assets)}"
            )
        )

    def _account_balance_line(self, balance: AccountBalance) -> str:
        kind = "负债" if balance.account_type is AccountType.LIABILITY else "资产"
        flags = [kind]
        if balance.archived:
            flags.append("已归档")
        return (
            f"• {balance.account_name}（{' · '.join(flags)}）"
            f"余额 {self._format_money(balance.current_balance)}"
        )
