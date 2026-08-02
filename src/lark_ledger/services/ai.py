import base64
from datetime import datetime
from typing import Any

import httpx

from lark_ledger.config import Settings
from lark_ledger.schemas import ParsedCommand

SYSTEM_PROMPT = """你是飞账的记账意图解析器。只理解用户输入，不保存数据、不生成或执行 SQL。
当前时间：{now}；时区：{timezone}；默认币种：{currency}。
将输入解析为一个 JSON 对象，严格遵守给定 schema。

动作：
- create：新增收支，必须给出 amount、direction、category、occurred_at。
- update_last：修改该用户最近一笔，仅填写要改变的字段。
- undo_last：撤销最近一笔。
- summary：消费/收入汇总，给出左闭右开的 range_start、range_end，可用 category 筛选。
- help：无法确认意图或缺少关键金额时使用。

分类使用简短中文，例如：餐饮、交通、购物、居住、娱乐、医疗、教育、工资、奖金、其他。
金额始终为正数；收入/支出由 direction 表示。不要臆造不明确的金额。
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
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "ledger_command",
                    "strict": True,
                    "schema": ParsedCommand.model_json_schema(),
                },
            },
        }
        response = await self._request("/chat/completions", json=payload)
        content = response["choices"][0]["message"]["content"]
        return ParsedCommand.model_validate_json(content)

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
