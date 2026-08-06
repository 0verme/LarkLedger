"""Explicit administrative CLI for guarded maintenance actions."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime

from lark_ledger.config import get_settings
from lark_ledger.db import SessionFactory, engine
from lark_ledger.models import PendingCommand
from lark_ledger.services.cleanup import CleanupStore
from lark_ledger.services.event_replay import EventReplayService
from lark_ledger.services.pending import PendingCommandStore, PendingPreview


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m lark_ledger.admin")
    subparsers = parser.add_subparsers(dest="command", required=True)
    replay = subparsers.add_parser(
        "replay-event",
        help="preflight a failed event; use --execute to atomically requeue it",
    )
    replay.add_argument("--event-id", required=True)
    replay.add_argument("--operator", required=True)
    replay.add_argument("--reason", required=True)
    replay.add_argument(
        "--execute",
        action="store_true",
        help="execute after a locked preflight (default is dry-run)",
    )
    list_pending = subparsers.add_parser(
        "list-pending",
        help="list pending confirmations with safe preview aggregates",
    )
    list_pending.add_argument("--limit", type=int, default=50)
    expire_pending = subparsers.add_parser(
        "expire-pending",
        help="mark due pending confirmations as expired (idempotent sweep)",
    )
    expire_pending.add_argument("--batch-size", type=int, default=500)
    return parser


def _pending_to_safe_dict(pending: PendingCommand) -> dict[str, object]:
    """Safe admin view of a pending: aggregates only, never the frozen payload."""
    preview = PendingPreview.from_json(pending.preview_json)
    return {
        "confirmation_code": pending.confirmation_code,
        "status": pending.status,
        "risk_reason": pending.risk_reason,
        "source_type": pending.source_type,
        "entries_total": preview.entries_total,
        "income_total": preview.income_total,
        "expense_total": preview.expense_total,
        "expires_at": pending.expires_at.isoformat() if pending.expires_at else None,
        "created_at": pending.created_at.isoformat() if pending.created_at else None,
    }


async def _run(args: argparse.Namespace) -> int:
    try:
        if args.command == "replay-event":
            result = await EventReplayService(SessionFactory).replay(
                args.event_id,
                operator=args.operator,
                reason=args.reason,
                execute=args.execute,
            )
            print(
                json.dumps(
                    result.to_safe_dict(), ensure_ascii=False, indent=2, sort_keys=True
                )
            )
            return 0 if result.outcome in {"eligible", "requeued"} else 2
        if args.command == "list-pending":
            pendings = await PendingCommandStore(
                SessionFactory, get_settings()
            ).list_all(limit=args.limit)
            items = [_pending_to_safe_dict(p) for p in pendings]
            print(
                json.dumps(
                    {"status": "ok", "count": len(items), "pending": items},
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "expire-pending":
            now = datetime.now(UTC)
            expired = await CleanupStore(SessionFactory).expire_pending_batch(
                cutoff=now, now=now, batch_size=args.batch_size
            )
            print(
                json.dumps(
                    {"status": "ok", "expired": expired},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        raise ValueError(f"unsupported command: {args.command}")
    except ValueError as exc:
        print(json.dumps({"status": "invalid_request", "error": str(exc)}, ensure_ascii=False))
        return 2
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
