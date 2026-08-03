import asyncio
from decimal import Decimal

import httpx
import pytest

from lark_ledger.config import Settings
from lark_ledger.services.exchange import ExchangeRateService, ExchangeRateUnavailableError


async def test_convert_uses_decimal_rounding_and_cache() -> None:
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        assert request.url.path == "/v2/rate/JPY/CNY"
        return httpx.Response(
            200,
            content=b'{"date":"2026-08-03","base":"JPY","quote":"CNY","rate":0.04781}',
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://rates.example"
    )
    service = ExchangeRateService(Settings(_env_file=None), client)
    assert await service.convert(Decimal("1300"), "JPY", "CNY") == Decimal("62.15")
    assert await service.convert(Decimal("100"), "JPY", "CNY") == Decimal("4.78")
    assert requests == 1
    await client.aclose()


async def test_same_currency_does_not_make_a_request() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("same-currency conversion must not call the API")

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://rates.example"
    )
    service = ExchangeRateService(Settings(_env_file=None), client)
    assert await service.convert(Decimal("1.235"), "CNY", "CNY") == Decimal("1.24")
    await client.aclose()


async def test_concurrent_requests_share_one_refresh() -> None:
    requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        await asyncio.sleep(0)
        return httpx.Response(
            200,
            json={"date": "2026-08-03", "base": "USD", "quote": "CNY", "rate": 7.2},
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://rates.example"
    )
    service = ExchangeRateService(Settings(_env_file=None), client)
    assert await asyncio.gather(*[service.rate("USD", "CNY") for _ in range(5)]) == [
        Decimal("7.2")
    ] * 5
    assert requests == 1
    await client.aclose()


async def test_expired_cache_is_used_when_refresh_fails() -> None:
    now = [0.0]
    available = [True]

    async def handler(request: httpx.Request) -> httpx.Response:
        if available[0]:
            return httpx.Response(
                200,
                json={"date": "2026-08-03", "base": "USD", "quote": "CNY", "rate": 7.2},
            )
        return httpx.Response(503)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://rates.example"
    )
    service = ExchangeRateService(
        Settings(_env_file=None, exchange_rate_cache_ttl_seconds=60),
        client,
        clock=lambda: now[0],
    )
    assert await service.rate("USD", "CNY") == Decimal("7.2")
    now[0] = 61
    available[0] = False
    assert await service.rate("USD", "CNY") == Decimal("7.2")
    await client.aclose()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503),
        httpx.Response(200, json={"base": "JPY", "quote": "CNY", "rate": -1}),
        httpx.Response(200, json={"base": "USD", "quote": "CNY", "rate": 7}),
    ],
)
async def test_first_unavailable_or_invalid_response_fails(response: httpx.Response) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: response),
        base_url="https://rates.example",
    )
    service = ExchangeRateService(Settings(_env_file=None), client)
    with pytest.raises(ExchangeRateUnavailableError):
        await service.rate("JPY", "CNY")
    await client.aclose()
