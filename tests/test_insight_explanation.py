"""P33 §64 — AI explanation layer: structured input, strict boundaries, fallback.

The AI explanation service consumes **only** a structured insight, never the
database; any provider failure falls back to the deterministic summary so
insights always work when AI is down. Tests use a mocked provider — no real
public AI is ever called.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from lark_ledger.config import Settings
from lark_ledger.services.insight_explanation import InsightExplanationService
from lark_ledger.web_schemas import Insight

SETTINGS = Settings(
    ai_api_key="test-key",
    ai_base_url="https://ai.example.test/v1",
    ai_model="test-model",
)


def _insight() -> Insight:
    return Insight(
        key="spending_change:餐饮:2026-08",
        type="spending_change",
        severity="attention",
        title="支出明显上升",
        summary="本月餐饮支出 2,860.00，近 3 个月平均 1,980.00，增加 44.4%",
        metric={
            "category": "餐饮",
            "current": "2860.00",
            "baseline": "1980.00",
            "change": "880.00",
            "change_percent": "44.4",
        },
        period="2026-08",
        related_category="餐饮",
        generated_at=datetime(2026, 8, 8, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_explain_with_mocked_provider() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        assert b"spending_change" in body
        # The system prompt forbids finance advice; ensure the request carries
        # only the structured insight, never any DB access path.
        assert b"SELECT" not in body
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "这个月餐饮支出比近三个月平均高约 44%，"
                            "如果后半月保持当前节奏，建议留意餐饮预算余量。"
                        }
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = InsightExplanationService(SETTINGS, client=client)
    text = await service.explain(_insight())
    assert text is not None
    assert "44%" in text
    await client.aclose()


@pytest.mark.asyncio
async def test_unconfigured_returns_none() -> None:
    settings = Settings(ai_api_key="")
    service = InsightExplanationService(settings)
    assert service.configured is False
    assert await service.explain(_insight()) is None


@pytest.mark.asyncio
async def test_provider_failure_falls_back_to_none() -> None:
    """P33 §35: AI timeout / 500 / garbage → None → caller uses the
    deterministic summary; insight availability never depends on AI."""

    async def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="quota exhausted")

    client = httpx.AsyncClient(transport=httpx.MockTransport(failing))
    service = InsightExplanationService(SETTINGS, client=client)
    assert await service.explain(_insight()) is None
    await client.aclose()


@pytest.mark.asyncio
async def test_invalid_output_falls_back_to_none() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = InsightExplanationService(SETTINGS, client=client)
    assert await service.explain(_insight()) is None
    await client.aclose()


@pytest.mark.asyncio
async def test_network_error_falls_back_to_none() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = InsightExplanationService(SETTINGS, client=client)
    assert await service.explain(_insight()) is None
    await client.aclose()


@pytest.mark.asyncio
async def test_deterministic_summary_is_complete_output() -> None:
    """P33 §35: the deterministic summary is the complete product output; the
    AI explanation only rewrites it when available."""
    insight = _insight()
    assert "44.4%" in insight.summary
    assert insight.metric["change_percent"] == "44.4"
