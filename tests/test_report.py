import io
import json
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx
from PIL import Image

from lark_ledger.config import Settings
from lark_ledger.schemas import AdviceResult, CategoryTotal, ReportData, TrendPoint
from lark_ledger.services.ai import AIInterpreter
from lark_ledger.services.report import ReportRenderer, build_report_card, fallback_advice


def sample_report(*, expense: Decimal = Decimal("75.50")) -> ReportData:
    return ReportData(
        range_start=datetime(2026, 8, 1, tzinfo=UTC),
        range_end=datetime(2026, 9, 1, tzinfo=UTC),
        currency="CNY",
        income_total=Decimal("1000"),
        expense_total=expense,
        balance=Decimal("1000") - expense,
        entry_count=3,
        categories=(
            [CategoryTotal(category="很长的餐饮分类名称", amount=expense)] if expense else []
        ),
        trend=[TrendPoint(period=date(2026, 8, 2), amount=expense)] if expense else [],
        trend_granularity="day",
    )


def test_report_renderer_creates_fixed_size_png() -> None:
    png = ReportRenderer().render(
        sample_report(),
        AdviceResult(items=["控制高频消费。", "保持稳定结余。"]),
    )
    image = Image.open(io.BytesIO(png))
    assert image.format == "PNG"
    assert image.size == (1200, 1600)


def test_report_renderer_handles_income_only_and_text_card_fallback() -> None:
    report = sample_report(expense=Decimal("0"))
    advice = fallback_advice(report)
    assert len(advice.items) >= 2
    assert ReportRenderer().render(report, advice).startswith(b"\x89PNG")

    card = build_report_card(report, "收入 ¥1000.00", advice=advice)
    assert card["schema"] == "2.0"
    assert all(element["tag"] != "img" for element in card["body"]["elements"])


async def test_advice_request_contains_only_aggregate_report_data() -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": '{"items":["减少冲动消费。","保持储蓄习惯。"]}'}},
                ]
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://ai.example/v1"
    )
    result = await AIInterpreter(Settings(ai_api_key="test-key"), client).generate_advice(
        sample_report()
    )
    await client.aclose()

    user_content = captured["messages"][1]["content"]  # type: ignore[index]
    assert isinstance(user_content, str)
    assert "income_total" in user_content
    assert "user_open_id" not in user_content
    assert "note" not in user_content
    assert result.items[0] == "减少冲动消费。"
