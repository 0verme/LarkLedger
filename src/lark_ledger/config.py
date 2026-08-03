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

    lark_app_id: str = ""
    lark_app_secret: str = ""
    lark_verification_token: str = ""
    lark_encrypt_key: str = ""
    lark_base_url: str = "https://open.feishu.cn"

    ai_api_key: str = ""
    ai_base_url: str = "https://api.openai.com/v1"
    ai_model: str = "gpt-4.1-mini"
    transcription_model: str = "gpt-4o-mini-transcribe"
    ai_timeout_seconds: float = Field(default=45, gt=0, le=180)
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
