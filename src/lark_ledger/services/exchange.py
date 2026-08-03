import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import httpx

from lark_ledger.config import Settings

MONEY_QUANTUM = Decimal("0.01")


class ExchangeRateUnavailableError(RuntimeError):
    """Raised when no usable exchange rate is available for a currency pair."""


@dataclass(frozen=True)
class _CachedRate:
    rate: Decimal
    fetched_at: float


class ExchangeRateService:
    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self._client = client
        self._clock = clock
        self._cache: dict[tuple[str, str], _CachedRate] = {}
        self._lock = asyncio.Lock()

    async def convert(self, amount: Decimal, source: str, target: str) -> Decimal:
        source = source.upper()
        target = target.upper()
        if source == target:
            return amount.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
        rate = await self.rate(source, target)
        return (amount * rate).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)

    async def rate(self, source: str, target: str) -> Decimal:
        key = (source.upper(), target.upper())
        cached = self._cache.get(key)
        if cached is not None and self._is_fresh(cached):
            return cached.rate

        async with self._lock:
            cached = self._cache.get(key)
            if cached is not None and self._is_fresh(cached):
                return cached.rate
            try:
                rate = await self._fetch_rate(*key)
            except (httpx.HTTPError, InvalidOperation, KeyError, TypeError, ValueError) as exc:
                if cached is not None:
                    return cached.rate
                raise ExchangeRateUnavailableError(
                    f"exchange rate unavailable for {key[0]}/{key[1]}"
                ) from exc
            self._cache[key] = _CachedRate(rate=rate, fetched_at=self._clock())
            return rate

    def _is_fresh(self, cached: _CachedRate) -> bool:
        return self._clock() - cached.fetched_at < self.settings.exchange_rate_cache_ttl_seconds

    async def _fetch_rate(self, source: str, target: str) -> Decimal:
        path = f"/v2/rate/{source}/{target}"
        if self._client is not None:
            response = await self._client.get(path)
        else:
            async with httpx.AsyncClient(
                base_url=self.settings.exchange_rate_api_url.rstrip("/"), timeout=10
            ) as client:
                response = await client.get(path)
        response.raise_for_status()
        payload: Any = json.loads(response.content, parse_float=Decimal)
        if not isinstance(payload, dict):
            raise ValueError("exchange rate response is not an object")
        if payload.get("base") != source or payload.get("quote") != target:
            raise ValueError("exchange rate response has an unexpected currency pair")
        rate = Decimal(str(payload["rate"]))
        if not rate.is_finite() or rate <= 0:
            raise ValueError("exchange rate must be finite and positive")
        return rate
