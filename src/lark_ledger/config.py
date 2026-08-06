from enum import StrEnum
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class EventMode(StrEnum):
    WEBHOOK = "webhook"
    WEBSOCKET = "websocket"


class Settings(BaseSettings):
    """Runtime configuration loaded from LARK_LEDGER_* environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="LARK_LEDGER_", env_file=".env", extra="ignore", case_sensitive=False
    )

    database_url: str = "postgresql+asyncpg://lark_ledger:change-me@db:5432/lark_ledger"
    timezone: str = "Asia/Shanghai"
    currency: str = "CNY"
    event_mode: EventMode = EventMode.WEBHOOK

    # Event worker (P05b): background PostgreSQL-driven worker with lease, retry,
    # and dead-letter handling. When enabled, entry points only claim events and
    # the worker processes them; when disabled, the legacy synchronous path runs.
    worker_enabled: bool = True
    worker_poll_interval_seconds: float = Field(default=1.0, gt=0, le=3600)
    worker_batch_size: int = Field(default=10, ge=1, le=100)
    event_max_attempts: int = Field(default=3, ge=1, le=100)
    event_lease_seconds: float = Field(default=300.0, gt=0, le=86400)
    event_retry_base_seconds: float = Field(default=2.0, gt=0, le=86400)
    event_retry_max_seconds: float = Field(default=3600.0, gt=0, le=86400)

    lark_app_id: str = ""
    lark_app_secret: str = ""
    lark_verification_token: str = ""
    lark_encrypt_key: str = ""
    lark_base_url: str = "https://open.feishu.cn"

    ai_api_key: str = ""
    ai_base_url: str = "https://api.openai.com/v1"
    ai_model: str = "gpt-4.1-mini"
    vision_api_key: str = ""
    vision_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    vision_model: str = "qwen3.7-plus"
    transcription_api_key: str = ""
    transcription_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    transcription_model: str = "qwen3-asr-flash"
    transcription_language: str = "zh"
    transcription_enable_itn: bool = True
    ai_timeout_seconds: float = Field(default=45, gt=0, le=180)
    exchange_rate_api_url: str = "https://api.frankfurter.dev"
    exchange_rate_cache_ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    report_font_path: str | None = None

    @field_validator("event_mode", mode="before")
    @classmethod
    def normalize_event_mode(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone: {value}") from exc
        return value

    @field_validator("currency")
    @classmethod
    def valid_currency(cls, value: str) -> str:
        value = value.upper()
        if len(value) != 3 or not value.isalpha():
            raise ValueError("currency must be a three-letter ISO 4217 code")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
