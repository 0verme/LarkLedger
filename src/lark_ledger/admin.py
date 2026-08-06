"""Explicit administrative CLI for guarded maintenance actions."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence

from lark_ledger.db import SessionFactory, engine
from lark_ledger.services.event_replay import EventReplayService


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
    return parser


async def _run(args: argparse.Namespace) -> int:
    try:
        if args.command != "replay-event":
            raise ValueError(f"unsupported command: {args.command}")
        result = await EventReplayService(SessionFactory).replay(
            args.event_id,
            operator=args.operator,
            reason=args.reason,
            execute=args.execute,
        )
        print(json.dumps(result.to_safe_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result.outcome in {"eligible", "requeued"} else 2
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
