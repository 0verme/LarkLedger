import base64
import json
import logging
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ValidationError

from lark_ledger.config import Settings
from lark_ledger.schemas import (
    MAX_BATCH_BUDGETS,
    MAX_BATCH_ENTRIES,
    AdviceResult,
    ParsedCommand,
    ReportData,
)

logger = logging.getLogger(__name__)


class CommandInterpretationError(ValueError):
    """Raised when an AI response does not match the allowed command schema."""


SYSTEM_PROMPT = """你是飞账的记账意图解析器。只理解用户输入，不保存数据、不生成或执行 SQL。
当前时间：{now}；时区：{timezone}；默认币种：{currency}。
将输入解析为一个 JSON 对象，严格遵守给定 schema。

动作：
- create：新增收支，必须给出 amount、direction、category、occurred_at。
  用户指定账户（如“用支付宝付了”“转到信用卡”）时填写 account_hint 为账户名称；
  账户由服务端在当前账本解析并校验，禁止返回或臆造 account_id。
- batch：文字消息包含多笔收支，或同时包含收支和预算设置时使用。把每笔收支按原文顺序放入
  entries，最多 {max_batch_entries} 笔；把预算放入 budgets，最多 {max_batch_budgets} 项。
  超出上限时分别设置 batch_truncated
  或 budgets_truncated。batch 不得包含修改、撤销、查询或报告动作，这些动作需要用户单独发送。
- create_entries：仅用于图片中存在多笔独立支付流水时，把每笔交易按图片顺序放入 entries，
  最多返回前 {max_batch_entries} 笔；图片中还有更多交易时将 batch_truncated 设为 true。
  逐项保留图片明确显示的
  amount、currency、direction、category、note、occurred_at，缺失字段留空，不要臆造。
- update_last：修改该用户最近一笔（无短 ID），仅填写要改变的字段；清空备注时 clear_note=true；
  修改账户时填写 account_hint。
- undo_last：撤销（软删除）最近一笔（无短 ID）。
- list_entries：查看逐笔账目列表（最近 N 笔、本月账单、某分类账单、分页）。
  可用 limit（1～20，默认 10）、可选 range_start/range_end（左闭右开）、
  category、direction；翻页时填写 before_entry_ref 为上一页最后一笔短 ID（必须来自用户消息）。
  示例：“最近10笔”“查看本月账单”“查看餐饮账单”“查看 #A83F2 之前的10笔”。
- get_entry：查看单笔详情，entry_ref 必须是用户消息中出现的五位短 ID。
- update_entry：按短 ID 修改指定账目。必须 entry_ref（来自用户消息），并至少改一个字段：
  amount、direction、category、note、occurred_at、account_hint；清空备注用 clear_note=true 且
  note 留空。对照：“把上一笔改成35元”→ update_last；“把 #A83F2 改成35元”→ update_entry。
- delete_entry：按短 ID 软删除，必须 entry_ref。对照：“撤销刚才那笔”→ undo_last；
  “删除 #A83F2”→ delete_entry。
- restore_entry：按短 ID 恢复已删除账目，必须 entry_ref。示例：“恢复 #A83F2”。
- list_accounts：查看账户列表或单账户余额。不带账户名时列出当前账本全部账户及余额；
  带明确账户名时填写 account_hint 查询单账户余额。示例：“查看账户”“账户列表”
  “支付宝余额”“查看信用卡余额”。
- assets：查询总资产、总负债、净资产。示例：“总资产”“净资产”“我现在欠多少负债”。
- export_entries：导出用户本人账目为 CSV 文件（仅 CSV）。可选 range_start/range_end
  （左闭右开）；无日期且未要求全部时不要填日期，业务层默认最近 90 天。
  仅当用户明确说“全部/所有/完整历史”等时设 export_all=true（禁止仅因日期为空就导出全部）。
  仅当用户明确要求包含已删除记录时设 include_deleted=true。
  禁止输出文件名、路径、user_open_id、chat_id、message_id。
  对照：“导出本月账单/导出最近90天/导出全部账单”→ export_entries；
  “查看本月账单”→ list_entries；“本月花了多少钱”→ summary；“查看 #A83F2”→ get_entry。
- summary：询问花费多少、收入多少或分类合计，给出左闭右开的 range_start、range_end，
  可用 category 筛选。
  对照：“本月花了多少钱/本月餐饮总共多少”→ summary；
  “查看本月账单/列出本月餐饮记录”→ list_entries；“查看 #A83F2”→ get_entry；
  “导出本月账单”→ export_entries。
- report：要求生成报告、图表或消费分析，给出左闭右开的 range_start、range_end。
- set_budget：设置或修改长期生效的品类月预算，必须给出 amount 和 category。
- set_budgets：一条消息设置多个品类月预算时使用，把每个品类、金额和可选币种放入
  budgets；最多 {max_batch_budgets} 项。示例“交通预算500，人情往来预算1000”必须解析成两个候选项。
- list_budgets：查看月预算；查看指定品类时填写 category，否则留空。
- delete_budget：取消指定品类的月预算，必须给出 category。
- help：无法确认意图或缺少关键金额时使用。

复杂文字规则：以用户最后的修正为准，不要同时保留被纠正的旧金额；优惠或满减只记录实际
支付金额；垫付支出、朋友还款、AA 收款和公司报销按真实资金流水分别记录，不要相互抵消。
“聚餐 426 我先付，另外三个人每人转给我 106.5”必须输出四笔独立流水：一笔支出 426，
再展开为三笔收入 106.5；垫付支出和 AA 收款缺一不可。单条文字只有一笔收支时仍使用 create。

JSON 示例：用户输入“2025-01-02 晚餐 100，交通预算 500”，输出
{{"action":"batch","entries":[{{"amount":"100","currency":null,"direction":"expense",
"category":"餐饮","note":"晚餐","occurred_at":"2025-01-02T19:00:00+08:00"}}],
"budgets":[{{"category":"交通","amount":"500","currency":null}}],
"batch_truncated":false,"budgets_truncated":false}}。

图片规则：单笔支付详情或小票仍使用 create。只有支付宝、微信支付、银行账单等包含多笔独立交易
的流水列表才使用 create_entries。小票中的多个商品属于同一笔消费，不得拆成多笔。不要把月度
支出/收入合计、余额、优惠金额或统计卡片当成独立账目。
多张图片出现在同一条消息时，它们共同构成一次记账请求；结合用户正文理解图片，正文中明确的
补充或纠正优先于图片中的模糊信息，但不得臆造未提供的交易。多张图片可能是连续页面或重叠截图，
相同交易只记录一次；所有图片合计仍最多返回前 {max_batch_entries} 笔独立流水。

分类使用简短中文，例如：餐饮、交通、购物、居住、娱乐、医疗、教育、工资、奖金、其他。
金额始终为正数；收入/支出由 direction 表示。不要臆造不明确的金额。
金额明确带有币种时填写 currency，使用三字母代码：人民币 CNY、美元 USD、欧元 EUR、
日元 JPY、英镑 GBP、港币 HKD、韩元 KRW、澳元 AUD、加元 CAD、新加坡元 SGD。
没有明确币种时 currency 留空并按默认币种处理。currency 只用于 create、update_last 和
set_budget；批量账目和批量预算的币种写在各自候选项中。summary 和 report 始终使用默认币种。
list_entries / get_entry / delete_entry / restore_entry / export_entries 不使用 currency。
update_entry 的 currency 仅在同时给出 amount 时可用于外币约算，与 update_last 相同。
entry_ref 必须来自用户原文中的短 ID，禁止臆造。
export_entries 不得填写 amount、category、note、limit、entry_ref。
"""


SYSTEM_PROMPT += """

Transfer rules:
- transfer: movement between two named accounts. Return amount, occurred_at,
  from_account_hint and to_account_hint. Never return or invent account_id.
- A transfer has no direction or category and is not income or expense.
- If either account name is unclear, use help instead of guessing.
"""


class AIInterpreter:
    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        *,
        vision_client: httpx.AsyncClient | None = None,
        transcription_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self._client = client
        self._vision_client = vision_client
        self._transcription_client = transcription_client

    @property
    def vision_configured(self) -> bool:
        return bool(self.settings.vision_api_key.strip())

    @property
    def transcription_configured(self) -> bool:
        return bool(self.settings.transcription_api_key.strip())

    async def interpret(
        self,
        text: str,
        *,
        now: datetime,
        images: Sequence[bytes] | None = None,
    ) -> ParsedCommand:
        image_payloads = list(images or ())
        if not image_payloads and not self.settings.ai_api_key:
            raise RuntimeError("尚未配置 LARK_LEDGER_AI_API_KEY")
        if image_payloads and not self.vision_configured:
            raise RuntimeError("尚未配置 LARK_LEDGER_VISION_API_KEY")

        user_content: str | list[dict[str, Any]] = text
        if image_payloads:
            user_content = [{"type": "text", "text": text or "识别图片中的收支并记账"}]
            for image in image_payloads:
                media_type = self._detect_image_media_type(image)
                encoded = base64.b64encode(image).decode("ascii")
                user_content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{media_type};base64,{encoded}"},
                    }
                )

        api_key = self.settings.vision_api_key if image_payloads else self.settings.ai_api_key
        base_url = (
            self.settings.vision_base_url if image_payloads else self.settings.ai_base_url
        )
        model = self.settings.vision_model if image_payloads else self.settings.ai_model
        request_client = self._vision_client if image_payloads else self._client

        payload = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": SYSTEM_PROMPT.format(
                        now=now.isoformat(),
                        timezone=self.settings.timezone,
                        currency=self.settings.currency,
                        max_batch_entries=MAX_BATCH_ENTRIES,
                        max_batch_budgets=MAX_BATCH_BUDGETS,
                    ),
                },
                {"role": "user", "content": user_content},
            ],
            "temperature": 0,
            "response_format": self._response_format(
                ParsedCommand,
                "ledger_command",
                base_url=str(request_client.base_url) if request_client is not None else base_url,
            ),
        }
        if image_payloads and self._is_dashscope_url(base_url):
            payload["enable_thinking"] = False
        if not image_payloads and self._is_deepseek_url(base_url):
            payload["thinking"] = {"type": "disabled"}
            payload["max_tokens"] = 8192
        response = await self._request(
            "/chat/completions",
            api_key=api_key,
            base_url=base_url,
            client=request_client,
            json=payload,
        )
        try:
            content = response["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise TypeError("AI command response content is empty or not text")
            payload_data = json.loads(content)
            if not isinstance(payload_data, dict):
                raise TypeError("AI command response is not a JSON object")
            return ParsedCommand.model_validate_json(
                json.dumps(self._normalize_command_payload(payload_data, now=now))
            )
        except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValidationError) as exc:
            logger.exception("AI command response failed schema validation")
            raise CommandInterpretationError("AI command response is invalid") from exc

    @staticmethod
    def _normalize_command_payload(
        payload: dict[str, Any], *, now: datetime
    ) -> dict[str, Any]:
        normalized = payload.copy()
        # JSON-only providers occasionally emit null when the user omits a
        # date. Preserve the existing "book it now" behavior at the AI trust
        # boundary while leaving strict validation for every other field.
        if normalized.get("action") == "create" and not normalized.get("occurred_at"):
            normalized["occurred_at"] = now.isoformat()
        limits = (
            ("entries", MAX_BATCH_ENTRIES, "batch_truncated"),
            ("budgets", MAX_BATCH_BUDGETS, "budgets_truncated"),
        )
        for field, limit, truncated_field in limits:
            items = normalized.get(field)
            if items == []:
                normalized.pop(field)
            elif isinstance(items, list) and len(items) > limit:
                normalized[field] = items[:limit]
                normalized[truncated_field] = True
        return normalized

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
            "response_format": self._response_format(
                AdviceResult,
                "consumption_advice",
                base_url=(
                    str(self._client.base_url)
                    if self._client is not None
                    else self.settings.ai_base_url
                ),
            ),
        }
        response = await self._request(
            "/chat/completions",
            api_key=self.settings.ai_api_key,
            base_url=self.settings.ai_base_url,
            client=self._client,
            json=payload,
        )
        content = response["choices"][0]["message"]["content"]
        return AdviceResult.model_validate_json(content)

    def _response_format(
        self, model: type[BaseModel], name: str, *, base_url: str
    ) -> dict[str, Any]:
        if self._is_deepseek_url(base_url) or self._is_dashscope_url(base_url):
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
        if not self.transcription_configured:
            raise RuntimeError("尚未配置 LARK_LEDGER_TRANSCRIPTION_API_KEY")
        if not audio:
            raise ValueError("语音内容不能为空")
        media_type = self._audio_media_type(filename)
        encoded = base64.b64encode(audio).decode("ascii")
        asr_options: dict[str, Any] = {
            "enable_itn": self.settings.transcription_enable_itn,
        }
        if self.settings.transcription_language.strip():
            asr_options["language"] = self.settings.transcription_language.strip()
        response = await self._request(
            "/chat/completions",
            api_key=self.settings.transcription_api_key,
            base_url=self.settings.transcription_base_url,
            client=self._transcription_client,
            json={
                "model": self.settings.transcription_model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_audio",
                                "input_audio": {
                                    "data": f"data:{media_type};base64,{encoded}",
                                },
                            }
                        ],
                    }
                ],
                "stream": False,
                "asr_options": asr_options,
            },
        )
        try:
            text = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("语音转写接口返回格式无效") from exc
        if not isinstance(text, str) or not text.strip():
            raise ValueError("语音转写没有返回文本")
        return text.strip()

    @staticmethod
    def _detect_image_media_type(image: bytes) -> str:
        if not image:
            raise ValueError("图片内容不能为空")
        if image.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if image.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if len(image) >= 12 and image.startswith(b"RIFF") and image[8:12] == b"WEBP":
            return "image/webp"
        raise ValueError("图片格式不受支持，请使用 JPEG、PNG 或 WebP")

    @staticmethod
    def _audio_media_type(filename: str) -> str:
        suffix = Path(filename).suffix.lower()
        media_types = {
            ".opus": "audio/ogg",
            ".ogg": "audio/ogg",
            ".wav": "audio/wav",
            ".mp3": "audio/mpeg",
            ".m4a": "audio/mp4",
            ".flac": "audio/flac",
            ".aac": "audio/aac",
            ".amr": "audio/amr",
        }
        try:
            return media_types[suffix]
        except KeyError as exc:
            raise ValueError(f"不支持的语音文件格式：{suffix or '未知'}") from exc

    @staticmethod
    def _is_dashscope_url(base_url: str) -> bool:
        hostname = urlparse(base_url).hostname or ""
        return hostname == "dashscope.aliyuncs.com" or hostname.endswith(".maas.aliyuncs.com")

    @staticmethod
    def _is_deepseek_url(base_url: str) -> bool:
        hostname = urlparse(base_url).hostname or ""
        return hostname == "api.deepseek.com" or hostname.endswith(".deepseek.com")

    async def _request(
        self,
        path: str,
        *,
        api_key: str,
        base_url: str,
        client: httpx.AsyncClient | None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {api_key}"}
        if client is not None:
            response = await client.post(path, headers=headers, **kwargs)
        else:
            async with httpx.AsyncClient(
                base_url=base_url.rstrip("/"),
                timeout=self.settings.ai_timeout_seconds,
            ) as client:
                response = await client.post(path, headers=headers, **kwargs)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("AI 接口返回了无效 JSON")
        return result
