import pytest

from lark_ledger.admin import build_parser


def test_replay_event_cli_defaults_to_dry_run_and_requires_audit_identity() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "replay-event",
            "--event-id",
            "evt-1",
            "--operator",
            "operator",
            "--reason",
            "dependency recovered",
        ]
    )
    assert args.command == "replay-event"
    assert args.execute is False

    executed = parser.parse_args(
        [
            "replay-event",
            "--event-id",
            "evt-1",
            "--operator",
            "operator",
            "--reason",
            "dependency recovered",
            "--execute",
        ]
    )
    assert executed.execute is True


@pytest.mark.parametrize("missing", ["operator", "reason"])
def test_replay_event_cli_rejects_missing_required_fields(missing: str) -> None:
    arguments = ["replay-event", "--event-id", "evt-1"]
    if missing != "operator":
        arguments.extend(["--operator", "operator"])
    if missing != "reason":
        arguments.extend(["--reason", "reason"])
    with pytest.raises(SystemExit):
        build_parser().parse_args(arguments)


def test_list_pending_cli_parses() -> None:
    parser = build_parser()
    args = parser.parse_args(["list-pending"])
    assert args.command == "list-pending"
    assert args.limit == 50
    args = parser.parse_args(["list-pending", "--limit", "10"])
    assert args.limit == 10


def test_expire_pending_cli_parses() -> None:
    parser = build_parser()
    args = parser.parse_args(["expire-pending"])
    assert args.command == "expire-pending"
    assert args.batch_size == 500
    args = parser.parse_args(["expire-pending", "--batch-size", "100"])
    assert args.batch_size == 100


def test_reconcile_reply_outbox_cli_defaults_to_dry_run() -> None:
    args = build_parser().parse_args(
        ["reconcile-reply-outbox", "--before", "2026-08-07T10:00:00+08:00"]
    )
    assert args.command == "reconcile-reply-outbox"
    assert args.execute is False
    assert args.before.isoformat() == "2026-08-07T02:00:00+00:00"


def test_reconcile_reply_outbox_cli_requires_timezone() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["reconcile-reply-outbox", "--before", "2026-08-07T10:00:00"]
        )
