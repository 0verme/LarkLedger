"""P04 CSV export: schema, query isolation, serialization, limits, Feishu upload."""

from __future__ import annotations

import csv
import io
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from lark_ledger.config import Settings
from lark_ledger.models import Base, Direction, LedgerEntry
from lark_ledger.schemas import (
    DEFAULT_EXPORT_DAYS,
    MAX_EXPORT_BYTES,
    MAX_EXPORT_ROWS,
    Action,
    ExportFileResult,
    ParsedCommand,
)
from lark_ledger.services.export import (
    CSV_HEADERS,
    ExportTooLargeError,
    build_export_file,
    build_export_filename,
    entries_to_csv_bytes,
    format_csv_amount,
    sanitize_csv_cell,
)
from lark_ledger.services.feishu import (
    FeishuClient,
    MessageProcessor,
    _safe_export_filename,
    _write_export_temp_file,
)
from lark_ledger.services.ledger import LedgerService

TZ = ZoneInfo("Asia/Shanghai")


async def _create(
    session: AsyncSession,
    *,
    user: str,
    short_id: str,
    amount: str,
    category: str,
    occurred_at: datetime,
    direction: Direction = Direction.EXPENSE,
    note: str = "",
    deleted: bool = False,
    created_at: datetime | None = None,
    currency: str = "CNY",
    source_type: str = "text",
) -> LedgerEntry:
    entry = LedgerEntry(
        user_open_id=user,
        short_id=short_id,
        amount=Decimal(amount),
        currency=currency,
        direction=direction,
        category=category,
        note=note,
        occurred_at=occurred_at,
        source_type=source_type,
        deleted_at=occurred_at if deleted else None,
    )
    session.add(entry)
    await session.flush()
    if created_at is not None:
        entry.created_at = created_at
        entry.updated_at = created_at
    await session.commit()
    await session.refresh(entry)
    return entry


def _parse_csv(content: bytes) -> list[list[str]]:
    assert content.startswith(b"\xef\xbb\xbf"), "UTF-8 BOM required"
    text = content.decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text)))


# --- Schema / Action ---------------------------------------------------------


def test_export_entries_schema_defaults_and_fields() -> None:
    bare = ParsedCommand(action=Action.EXPORT_ENTRIES)
    assert bare.export_all is False
    assert bare.include_deleted is False
    assert bare.range_start is None
    assert bare.range_end is None

    ranged = ParsedCommand(
        action=Action.EXPORT_ENTRIES,
        range_start=datetime(2026, 1, 1, tzinfo=UTC),
        range_end=datetime(2026, 7, 1, tzinfo=UTC),
    )
    assert ranged.range_start is not None

    full = ParsedCommand(action=Action.EXPORT_ENTRIES, export_all=True)
    assert full.export_all is True

    with_deleted = ParsedCommand(
        action=Action.EXPORT_ENTRIES, include_deleted=True, export_all=True
    )
    assert with_deleted.include_deleted is True


def test_export_entries_rejects_invalid_range_and_foreign_fields() -> None:
    with pytest.raises(ValidationError):
        ParsedCommand(
            action=Action.EXPORT_ENTRIES,
            range_start=datetime(2026, 8, 1, tzinfo=UTC),
            range_end=datetime(2026, 7, 1, tzinfo=UTC),
        )
    with pytest.raises(ValidationError):
        ParsedCommand(
            action=Action.EXPORT_ENTRIES,
            range_start=datetime(2026, 8, 1, tzinfo=UTC),
        )
    with pytest.raises(ValidationError):
        ParsedCommand(action=Action.EXPORT_ENTRIES, amount=Decimal("1"))
    with pytest.raises(ValidationError):
        ParsedCommand(action=Action.EXPORT_ENTRIES, entry_ref="A83F2")
    with pytest.raises(ValidationError):
        ParsedCommand(action=Action.EXPORT_ENTRIES, limit=10)
    with pytest.raises(ValidationError):
        ParsedCommand(action=Action.LIST_ENTRIES, export_all=True)
    with pytest.raises(ValidationError):
        ParsedCommand(action=Action.SUMMARY, include_deleted=True)


def test_list_and_summary_remain_distinct_from_export() -> None:
    listing = ParsedCommand(action=Action.LIST_ENTRIES)
    assert listing.action is Action.LIST_ENTRIES
    summary = ParsedCommand(
        action=Action.SUMMARY,
        range_start=datetime(2026, 8, 1, tzinfo=UTC),
        range_end=datetime(2026, 9, 1, tzinfo=UTC),
    )
    assert summary.action is Action.SUMMARY
    export = ParsedCommand(action=Action.EXPORT_ENTRIES)
    assert export.action is Action.EXPORT_ENTRIES


# --- CSV sanitize / format ---------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("=1+1", "'=1+1"),
        ("+cmd", "'+cmd"),
        ("-2+3", "'-2+3"),
        ("@SUM(A1:A2)", "'@SUM(A1:A2)"),
        ("   =HYPERLINK(\"x\")", "'   =HYPERLINK(\"x\")"),
        ("\t=1+1", "'\t=1+1"),
        ("\r=1+1", "'\r=1+1"),
        ("正常备注", "正常备注"),
        ("", ""),
        ("午餐,外卖", "午餐,外卖"),
    ],
)
def test_sanitize_csv_cell_formula_injection(raw: str, expected: str) -> None:
    assert sanitize_csv_cell(raw) == expected


def test_format_csv_amount_stable_decimal() -> None:
    assert format_csv_amount(Decimal("12.5")) == "12.50"
    assert format_csv_amount(Decimal("1000")) == "1000.00"
    assert "E" not in format_csv_amount(Decimal("0.01"))


def test_csv_content_headers_escaping_bom_and_no_secrets() -> None:
    when = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    entry = LedgerEntry(
        id=uuid.uuid4(),
        user_open_id="ou_secret_user",
        short_id="A83F2",
        amount=Decimal("12.50"),
        currency="CNY",
        direction=Direction.EXPENSE,
        category="餐饮",
        note='他说："好", 再来\n第二行 🍜',
        occurred_at=when,
        source_type="text",
        source_message_id="om_secret_msg",
        created_at=when,
        updated_at=when,
        deleted_at=None,
    )
    payload = entries_to_csv_bytes([entry], timezone=TZ)
    rows = _parse_csv(payload)
    assert rows[0] == list(CSV_HEADERS)
    assert len(rows) == 2
    data = rows[1]
    assert data[0] == "#A83F2"
    assert data[2] == "expense"
    assert data[3] == "12.50"
    assert data[5] == "餐饮"
    assert "好" in data[6]
    assert "🍜" in data[6]
    assert "\n" in data[6] or "第二行" in data[6]
    body = payload.decode("utf-8-sig")
    assert "ou_secret_user" not in body
    assert "om_secret_msg" not in body
    assert str(entry.id) not in body
    assert "user_open_id" not in body


def test_csv_injection_does_not_mutate_entry_fields() -> None:
    when = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    entry = LedgerEntry(
        id=uuid.uuid4(),
        user_open_id="ou_a",
        short_id="NJ001",
        amount=Decimal("1"),
        currency="CNY",
        direction=Direction.EXPENSE,
        category="=1+1",
        note="+cmd",
        occurred_at=when,
        source_type="text",
        created_at=when,
        updated_at=when,
    )
    original_category = entry.category
    original_note = entry.note
    payload = entries_to_csv_bytes([entry], timezone=TZ)
    rows = _parse_csv(payload)
    assert rows[1][5] == "'=1+1"
    assert rows[1][6] == "'+cmd"
    assert entry.category == original_category
    assert entry.note == original_note


def test_build_export_filename_has_v1_no_user_input() -> None:
    name = build_export_filename(datetime(2026, 8, 5, 22, 30, 0, tzinfo=TZ))
    assert name.startswith("larkledger-export-v1-")
    assert name.endswith(".csv")
    assert "ou_" not in name
    assert "#" not in name


def test_export_rejects_oversized_csv() -> None:
    when = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    entry = LedgerEntry(
        id=uuid.uuid4(),
        user_open_id="ou_a",
        short_id="BG001",
        amount=Decimal("1"),
        currency="CNY",
        direction=Direction.EXPENSE,
        category="x",
        note="n" * 200,
        occurred_at=when,
        source_type="text",
        created_at=when,
        updated_at=when,
    )
    with pytest.raises(ExportTooLargeError):
        entries_to_csv_bytes([entry], timezone=TZ, max_bytes=20)


# --- Ledger query / isolation ------------------------------------------------


async def test_export_default_90_days_excludes_deleted_and_others(
    session: AsyncSession,
) -> None:
    now = datetime(2026, 8, 5, 12, tzinfo=UTC)
    service = LedgerService(session, timezone="Asia/Shanghai", now=now)
    await _create(
        session,
        user="ou_a",
        short_id="AAA01",
        amount="10",
        category="餐饮",
        occurred_at=now - timedelta(days=10),
    )
    await _create(
        session,
        user="ou_a",
        short_id="AAA02",
        amount="20",
        category="交通",
        occurred_at=now - timedelta(days=10),
        deleted=True,
    )
    await _create(
        session,
        user="ou_a",
        short_id="AGE01",
        amount="30",
        category="购物",
        occurred_at=now - timedelta(days=DEFAULT_EXPORT_DAYS + 5),
    )
    await _create(
        session,
        user="ou_b",
        short_id="AAA01",
        amount="99",
        category="他户",
        occurred_at=now - timedelta(days=5),
        note="不应出现",
    )

    result = await service.execute("ou_a", ParsedCommand(action=Action.EXPORT_ENTRIES))
    assert result.export is not None
    assert result.export.row_count == 1
    body = result.export.content.decode("utf-8-sig")
    assert "#AAA01" in body
    assert "餐饮" in body
    assert "#AAA02" not in body
    assert "他户" not in body
    assert "不应出现" not in body
    assert "99" not in body
    assert "ou_a" not in body
    assert "ou_b" not in body
    assert "已导出 1 笔账目" in result.message


async def test_export_include_deleted_and_custom_range(session: AsyncSession) -> None:
    await _create(
        session,
        user="ou_a",
        short_id="XDE01",
        amount="5",
        category="餐饮",
        occurred_at=datetime(2026, 6, 15, tzinfo=UTC),
        deleted=True,
    )
    await _create(
        session,
        user="ou_a",
        short_id="XAK11",
        amount="6",
        category="交通",
        occurred_at=datetime(2026, 6, 16, tzinfo=UTC),
    )
    await _create(
        session,
        user="ou_a",
        short_id="XAT01",
        amount="7",
        category="购物",
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    result = await LedgerService(session).execute(
        "ou_a",
        ParsedCommand(
            action=Action.EXPORT_ENTRIES,
            range_start=datetime(2026, 6, 1, tzinfo=UTC),
            range_end=datetime(2026, 7, 1, tzinfo=UTC),
            include_deleted=True,
        ),
    )
    assert result.export is not None
    assert result.export.row_count == 2
    body = result.export.content.decode("utf-8-sig")
    assert "#XDE01" in body
    assert "#XAK11" in body
    assert "#XAT01" not in body


async def test_export_all_and_half_open_bounds(session: AsyncSession) -> None:
    start = datetime(2026, 5, 1, tzinfo=UTC)
    end = datetime(2026, 6, 1, tzinfo=UTC)
    await _create(
        session,
        user="ou_a",
        short_id="NS001",
        amount="1",
        category="餐饮",
        occurred_at=start,
    )
    await _create(
        session,
        user="ou_a",
        short_id="EDGE1",
        amount="2",
        category="餐饮",
        occurred_at=end,
    )
    await _create(
        session,
        user="ou_a",
        short_id="AGE99",
        amount="3",
        category="餐饮",
        occurred_at=datetime(2020, 1, 1, tzinfo=UTC),
    )

    ranged = await LedgerService(session).execute(
        "ou_a",
        ParsedCommand(
            action=Action.EXPORT_ENTRIES,
            range_start=start,
            range_end=end,
        ),
    )
    assert ranged.export is not None
    body = ranged.export.content.decode("utf-8-sig")
    assert "#NS001" in body
    assert "#EDGE1" not in body  # exclusive end

    full = await LedgerService(session).execute(
        "ou_a",
        ParsedCommand(action=Action.EXPORT_ENTRIES, export_all=True),
    )
    assert full.export is not None
    assert full.export.row_count == 3
    assert "全部历史" in full.message


async def test_export_stable_sort_and_empty(session: AsyncSession) -> None:
    base = datetime(2026, 8, 1, 10, tzinfo=UTC)
    # Same occurred_at; order by created_at then id.
    e1 = await _create(
        session,
        user="ou_a",
        short_id="S0001",
        amount="1",
        category="餐饮",
        occurred_at=base,
        created_at=base + timedelta(seconds=2),
    )
    e2 = await _create(
        session,
        user="ou_a",
        short_id="S0002",
        amount="2",
        category="餐饮",
        occurred_at=base,
        created_at=base + timedelta(seconds=1),
    )
    e3 = await _create(
        session,
        user="ou_a",
        short_id="S0003",
        amount="3",
        category="餐饮",
        occurred_at=base - timedelta(days=1),
        created_at=base,
    )

    result = await LedgerService(session).execute(
        "ou_a",
        ParsedCommand(action=Action.EXPORT_ENTRIES, export_all=True),
    )
    rows = _parse_csv(result.export.content)  # type: ignore[union-attr]
    ids = [row[0] for row in rows[1:]]
    assert ids == ["#S0003", "#S0002", "#S0001"]
    assert e1.short_id and e2.short_id and e3.short_id

    empty = await LedgerService(session).execute(
        "ou_b",
        ParsedCommand(action=Action.EXPORT_ENTRIES, export_all=True),
    )
    assert empty.export is None
    assert "没有可导出的账目" in empty.message


async def test_export_row_limit_rejects_without_truncation(session: AsyncSession) -> None:
    base = datetime(2026, 8, 1, tzinfo=UTC)
    limit = 3
    entries = [
        LedgerEntry(
            user_open_id="ou_a",
            short_id=f"{index:05d}",
            amount=Decimal("1.00"),
            currency="CNY",
            direction=Direction.EXPENSE,
            category="餐饮",
            note="",
            occurred_at=base + timedelta(seconds=index),
            source_type="text",
        )
        for index in range(limit + 1)
    ]
    session.add_all(entries)
    await session.commit()

    with patch("lark_ledger.services.ledger.MAX_EXPORT_ROWS", limit):
        result = await LedgerService(session).execute(
            "ou_a",
            ParsedCommand(action=Action.EXPORT_ENTRIES, export_all=True),
        )
        assert result.export is None
        assert f"超过 {limit}" in result.message
        assert "缩小" in result.message

        victim = (
            await session.execute(
                select(LedgerEntry)
                .where(LedgerEntry.user_open_id == "ou_a")
                .order_by(LedgerEntry.occurred_at.desc())
                .limit(1)
            )
        ).scalar_one()
        await session.delete(victim)
        await session.commit()

        ok = await LedgerService(session).execute(
            "ou_a",
            ParsedCommand(action=Action.EXPORT_ENTRIES, export_all=True),
        )
    assert ok.export is not None
    assert ok.export.row_count == limit


async def test_export_size_limit_message(session: AsyncSession) -> None:
    now = datetime(2026, 8, 5, 12, tzinfo=UTC)
    await _create(
        session,
        user="ou_a",
        short_id="SZ001",
        amount="1",
        category="餐饮",
        occurred_at=now,
        note="x" * 100,
    )
    with patch(
        "lark_ledger.services.ledger.build_export_file",
        side_effect=__import__(
            "lark_ledger.services.export", fromlist=["ExportTooLargeError"]
        ).ExportTooLargeError("too big"),
    ):
        result = await LedgerService(session, now=now).execute(
            "ou_a",
            ParsedCommand(action=Action.EXPORT_ENTRIES, export_all=True),
        )
    assert result.export is None
    assert "5MB" in result.message


async def test_export_is_read_only_no_revision_side_effects(session: AsyncSession) -> None:
    now = datetime(2026, 8, 5, 12, tzinfo=UTC)
    entry = await _create(
        session,
        user="ou_a",
        short_id="RD001",
        amount="1",
        category="餐饮",
        occurred_at=now,
    )
    before = entry.updated_at
    await LedgerService(session, now=now).execute(
        "ou_a",
        ParsedCommand(action=Action.EXPORT_ENTRIES, export_all=True),
    )
    await session.refresh(entry)
    assert entry.updated_at == before
    assert entry.deleted_at is None


# --- Feishu upload / cleanup -------------------------------------------------


async def test_upload_file_success_and_cleanup() -> None:
    requests: list[httpx.Request] = []
    seen_temp: list[Path] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "token"})
        if request.url.path.endswith("/files"):
            assert b"file_type" in request.content or b"stream" in request.content
            assert b"larkledger-export-v1" in request.content or b"export" in request.content
            return httpx.Response(200, json={"code": 0, "data": {"file_key": "file_1"}})
        return httpx.Response(200, json={"code": 0})

    original_write = _write_export_temp_file

    def tracking_write(content: bytes, safe_name: str) -> Path:
        path = original_write(content, safe_name)
        seen_temp.append(path)
        return path

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://open.feishu.cn"
    )
    feishu = FeishuClient(Settings(lark_app_id="app", lark_app_secret="secret"), client)
    content = b"\xef\xbb\xbfshort_id\n#A83F2\n"
    with patch("lark_ledger.services.feishu._write_export_temp_file", side_effect=tracking_write):
        key = await feishu.upload_file(content, "larkledger-export-v1-20260805-223000.csv")
    await client.aclose()
    assert key == "file_1"
    assert seen_temp
    assert not seen_temp[0].exists()


async def test_upload_file_failure_still_cleans_temp() -> None:
    seen_temp: list[Path] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "token"})
        return httpx.Response(200, json={"code": 999, "msg": "no permission"})

    original_write = _write_export_temp_file

    def tracking_write(content: bytes, safe_name: str) -> Path:
        path = original_write(content, safe_name)
        seen_temp.append(path)
        return path

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://open.feishu.cn"
    )
    feishu = FeishuClient(Settings(lark_app_id="app", lark_app_secret="secret"), client)
    with patch("lark_ledger.services.feishu._write_export_temp_file", side_effect=tracking_write):
        with pytest.raises(RuntimeError, match="上传飞书文件失败"):
            await feishu.upload_file(b"data", "larkledger-export-v1-x.csv")
    await client.aclose()
    assert seen_temp
    assert not seen_temp[0].exists()


def test_safe_export_filename_rejects_path_traversal() -> None:
    with pytest.raises(ValueError):
        _safe_export_filename("../evil.csv")
    with pytest.raises(ValueError):
        _safe_export_filename("a/b.csv")
    with pytest.raises(ValueError):
        _safe_export_filename("notcsv.txt")
    assert _safe_export_filename("larkledger-export-v1-20260805-223000.csv").endswith(".csv")


class ExportInterpreter:
    def __init__(self, command: ParsedCommand) -> None:
        self.command = command

    async def interpret(self, text: str, **kwargs: Any) -> ParsedCommand:
        return self.command


class RecordingExportFeishu:
    def __init__(
        self,
        *,
        upload_error: Exception | None = None,
        send_error: Exception | None = None,
    ) -> None:
        self.upload_error = upload_error
        self.send_error = send_error
        self.uploads: list[tuple[bytes, str]] = []
        self.files: list[str] = []
        self.texts: list[str] = []

    async def upload_file(self, content: bytes, filename: str) -> str:
        if self.upload_error:
            raise self.upload_error
        self.uploads.append((content, filename))
        return "file_export"

    async def reply_file(
        self, message_id: str, file_key: str, *, uuid: str | None = None
    ) -> None:
        if self.send_error:
            raise self.send_error
        self.files.append(file_key)

    async def reply_text(
        self, message_id: str, text: str, *, uuid: str | None = None
    ) -> None:
        self.texts.append(text)


async def _processor_session_factory() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)


async def test_message_processor_export_success_and_failure_paths() -> None:
    factory = await _processor_session_factory()
    async with factory() as session:
        await _create(
            session,
            user="ou_user",
            short_id="MSG01",
            amount="8",
            category="餐饮",
            occurred_at=datetime(2026, 8, 1, tzinfo=UTC),
        )

    settings = Settings(lark_app_id="app", lark_app_secret="secret")
    event = {
        "sender": {"sender_id": {"open_id": "ou_user"}},
        "message": {
            "message_id": "om_export",
            "message_type": "text",
            "content": '{"text":"导出全部账单"}',
        },
    }

    feishu_ok = RecordingExportFeishu()
    processor_ok = MessageProcessor(
        settings,
        factory,
        feishu_ok,  # type: ignore[arg-type]
        ExportInterpreter(ParsedCommand(action=Action.EXPORT_ENTRIES, export_all=True)),  # type: ignore[arg-type]
    )
    await processor_ok.process(event)
    assert feishu_ok.uploads
    assert feishu_ok.files == ["file_export"]
    assert any("已导出" in text for text in feishu_ok.texts)
    assert b"ou_user" not in feishu_ok.uploads[0][0]

    feishu_fail = RecordingExportFeishu(upload_error=RuntimeError("upload boom"))
    processor_fail = MessageProcessor(
        settings,
        factory,
        feishu_fail,  # type: ignore[arg-type]
        ExportInterpreter(ParsedCommand(action=Action.EXPORT_ENTRIES, export_all=True)),  # type: ignore[arg-type]
    )
    await processor_fail.process(event)
    assert not feishu_fail.files
    assert any("发送文件失败" in text for text in feishu_fail.texts)

    feishu_send_fail = RecordingExportFeishu(send_error=RuntimeError("send boom"))
    processor_send = MessageProcessor(
        settings,
        factory,
        feishu_send_fail,  # type: ignore[arg-type]
        ExportInterpreter(ParsedCommand(action=Action.EXPORT_ENTRIES, export_all=True)),  # type: ignore[arg-type]
    )
    await processor_send.process(event)
    assert any("发送文件失败" in text for text in feishu_send_fail.texts)


async def test_export_empty_result_only_text_no_upload() -> None:
    factory = await _processor_session_factory()
    settings = Settings(lark_app_id="app", lark_app_secret="secret")
    feishu = RecordingExportFeishu()
    processor = MessageProcessor(
        settings,
        factory,
        feishu,  # type: ignore[arg-type]
        ExportInterpreter(ParsedCommand(action=Action.EXPORT_ENTRIES, export_all=True)),  # type: ignore[arg-type]
    )
    await processor.process(
        {
            "sender": {"sender_id": {"open_id": "ou_empty"}},
            "message": {
                "message_id": "om_empty",
                "message_type": "text",
                "content": '{"text":"导出全部账单"}',
            },
        }
    )
    assert not feishu.uploads
    assert not feishu.files
    assert any("没有可导出" in text for text in feishu.texts)


def test_export_constants_centralized() -> None:
    assert MAX_EXPORT_ROWS == 5000
    assert MAX_EXPORT_BYTES == 5 * 1024 * 1024
    assert DEFAULT_EXPORT_DAYS == 90


def test_build_export_file_result_shape() -> None:
    when = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
    entry = LedgerEntry(
        id=uuid.uuid4(),
        user_open_id="ou_a",
        short_id="SHP01",
        amount=Decimal("3.10"),
        currency="CNY",
        direction=Direction.INCOME,
        category="工资",
        note="",
        occurred_at=when,
        source_type="text",
        created_at=when,
        updated_at=when,
    )
    result = build_export_file(
        [entry],
        timezone=TZ,
        when=datetime(2026, 8, 5, 22, 30, 0, tzinfo=TZ),
        range_label="全部历史",
    )
    assert isinstance(result, ExportFileResult)
    assert result.row_count == 1
    assert "v1" in result.filename
    rows = _parse_csv(result.content)
    assert rows[1][2] == "income"
    assert rows[1][3] == "3.10"
    assert rows[1][6] == ""
