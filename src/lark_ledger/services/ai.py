import base64
import json
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel

from lark_ledger.config import Settings
from lark_ledger.schemas import AdviceResult, ParsedCommand, ReportData

SYSTEM_PROMPT = """你是飞账的记账意图解析器。只理解用户输入，不保存数据、不生成或执行 SQL。
当前时间：{now}；时区：{timezone}；默认币种：{currency}。
将输入解析为一个 JSON 对象，严格遵守给定 schema。

动作：
- create：新增收支，必须给出 amount、direction、category、occurred_at。
- update_last：修改该用户最近一笔，仅填写要改变的字段。
- undo_last：撤销最近一笔。
- summary：询问花费多少、收入多少或分类汇总，给出左闭右开的 range_start、range_end，
  可用 category 筛选。
- report：要求生成报告、图表或消费分析，给出左闭右开的 range_start、range_end。
- set_budget：设置或修改长期生效的品类月预算，必须给出 amount 和 category。
- list_budgets：查看月预算；查看指定品类时填写 category，否则留空。
- delete_budget：取消指定品类的月预算，必须给出 category。
- help：无法确认意图或缺少关键金额时使用。

分类使用简短中文，例如：餐饮、交通、购物、居住、娱乐、医疗、教育、工资、奖金、其他。
金额始终为正数；收入/支出由 direction 表示。不要臆造不明确的金额。
金额明确带有币种时填写 currency，使用三字母代码：人民币 CNY、美元 USD、欧元 EUR、
日元 JPY、英镑 GBP、港币 HKD、韩元 KRW、澳元 AUD、加元 CAD、新加坡元 SGD。
没有明确币种时 currency 留空并按默认币种处理。currency 只用于 create、update_last 和
set_budget，且必须与 amount 同时出现；summary 和 report 始终使用默认币种。
"""


class AIInterpreter:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client

    async def interpret(
        self,
        text: str,
        *,
        now: datetime,
        image: bytes | None = None,
        image_media_type: str = "image/jpeg",
    ) -> ParsedCommand:
        if not self.settings.ai_api_key:
            raise RuntimeError("尚未配置 LARK_LEDGER_AI_API_KEY")

        user_content: str | list[dict[str, Any]] = text
        if image is not None:
            encoded = base64.b64encode(image).decode("ascii")
            user_content = [
                {"type": "text", "text": text or "识别图片中的收支并记账"},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{image_media_type};base64,{encoded}"},
                },
            ]

        payload = {
            "model": self.settings.ai_model,
            "messages": [
                {
                    "role": "system",
                    "content": SYSTEM_PROMPT.format(
                        now=now.isoformat(),
                        timezone=self.settings.timezone,
                        currency=self.settings.currency,
                    ),
                },
                {"role": "user", "content": user_content},
            ],
            "temperature": 0,
            "response_format": self._response_format(ParsedCommand, "ledger_command"),
        }
        response = await self._request("/chat/completions", json=payload)
        content = response["choices"][0]["message"]["content"]
        return ParsedCommand.model_validate_json(content)

    async def generate_advice(self, report: ReportData) -> AdviceResult:
        payload_data = {
            "range_start": report.range_start.isoformat(),
            "range_end": report.range_end.isoformat(),
            "currency": report.currency,
            "income_total": str(report.income_total),
            "expense_total": str(report.expense_total),
            "balance": str(report.balance),
            "entry_count": report.entry_count,
            "categories": [item.model_dump(mode="json") for item in report.categories],
            "trend": [item.model_dump(mode="json") for item in report.trend],
            "trend_granularity": report.trend_granularity,
        }
        payload = {
            "model": self.settings.ai_model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是克制、实用的消费分析助手。仅根据提供的聚合数据，给出 2 到 3 条"
                        "简短中文建议；不要臆测用户身份、职业或未提供的消费明细。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(payload_data, ensure_ascii=False),
                },
            ],
            "temperature": 0.2,
            "response_format": self._response_format(AdviceResult, "consumption_advice"),
        }
        response = await self._request("/chat/completions", json=payload)
        content = response["choices"][0]["message"]["content"]
        return AdviceResult.model_validate_json(content)

    def _response_format(
        self, model: type[BaseModel], name: str
    ) -> dict[str, Any]:
        base_url = (
            str(self._client.base_url)
            if self._client is not None
            else self.settings.ai_base_url
        )
        hostname = urlparse(base_url).hostname or ""
        if hostname == "api.deepseek.com" or hostname.endswith(".deepseek.com"):
            return {"type": "json_object"}
        return {
            "type": "json_schema",
            "json_schema": {
                "name": name,
                "strict": True,
                "schema": model.model_json_schema(),
            },
        }

    async def transcribe(self, audio: bytes, filename: str = "voice.opus") -> str:
        if not self.settings.ai_api_key:
            raise RuntimeError("尚未配置 LARK_LEDGER_AI_API_KEY")
        response = await self._request(
            "/audio/transcriptions",
            data={"model": self.settings.transcription_model},
            files={"file": (filename, audio, "application/octet-stream")},
        )
        text = response.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("语音转写没有返回文本")
        return text.strip()

    async def _request(self, path: str, **kwargs: Any) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.settings.ai_api_key}"}
        if self._client is not None:
            response = await self._client.post(path, headers=headers, **kwargs)
        else:
            async with httpx.AsyncClient(
                base_url=self.settings.ai_base_url.rstrip("/"),
                timeout=self.settings.ai_timeout_seconds,
            ) as client:
                response = await client.post(path, headers=headers, **kwargs)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("AI 接口返回了无效 JSON")
        return result
