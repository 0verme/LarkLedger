"""Optional AI explanation layer for insights (P33 §33–36).

The only place AI is allowed in P33. Strict boundaries:

* Input is **only** a structured ``Insight`` (already computed deterministically
  by ``InsightService``). The model can rewrite / explain — it can never access
  the database, generate SQL, query accounts or transactions, compute
  percentages, or modify anything.
* Any failure — no API key, timeout, invalid output, quota — falls back to the
  deterministic summary, so insights always work when AI is down.
* AI is never a startup dependency: readiness does not check AI health, and no
  worker waits on an AI call.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from pydantic import ValidationError

from lark_ledger.config import Settings
from lark_ledger.web_schemas import Insight

logger = logging.getLogger(__name__)

EXPLANATION_SYSTEM_PROMPT = (
    "你是飞账的克制型财务解释助手。只能基于给定的结构化洞察数据改写一段简短中文说明"
    "（2 到 3 句，不超过 80 字）。不要臆测用户身份、职业或未提供的明细；"
    "不要建议投资、理财、贷款或税务操作；不要计算任何数字——所有数字都来自输入。"
    "只输出解释文本本身，不要 JSON 包装。"
)


class InsightExplanationService:
    """Rewrite a deterministic insight into friendlier language.

    ``explain`` returns ``None`` whenever the explanation is unavailable so
    callers always fall back to ``Insight.summary``.
    """

    def __init__(
        self, settings: Settings, client: httpx.AsyncClient | None = None
    ) -> None:
        self.settings = settings
        self._client = client

    @property
    def configured(self) -> bool:
        return bool(self.settings.ai_api_key.strip())

    async def explain(self, insight: Insight) -> str | None:
        """Best-effort natural-language rewrite; ``None`` on any failure."""
        if not self.configured:
            return None
        payload_data = {
            "type": insight.type,
            "severity": insight.severity,
            "title": insight.title,
            "summary": insight.summary,
            "metric": insight.metric,
            "period": insight.period,
            "category": insight.related_category,
            "goal": insight.related_goal_name,
        }
        payload = {
            "model": self.settings.ai_model,
            "messages": [
                {"role": "system", "content": EXPLANATION_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(payload_data, ensure_ascii=False),
                },
            ],
            "temperature": 0.2,
        }
        try:
            response = await self._request(payload)
            content = response["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise TypeError("explanation content is empty or not text")
            return content.strip()
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValidationError, ValueError):
            logger.warning("insight AI explanation failed; falling back to summary", exc_info=True)
            return None

    async def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.settings.ai_api_key}"}
        url = f"{self.settings.ai_base_url.rstrip('/')}/chat/completions"
        if self._client is not None:
            response = await self._client.post(url, headers=headers, json=payload)
        else:
            async with httpx.AsyncClient(timeout=self.settings.ai_timeout_seconds) as client:
                response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("AI 接口返回了无效 JSON")
        return result
