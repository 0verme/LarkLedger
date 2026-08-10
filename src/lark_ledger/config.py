from enum import StrEnum
from functools import lru_cache
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator
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

    # Reply worker (P06b): background delivery of committed ``reply_outbox``
    # intents with lease, retry, and dead-lettering. When enabled, the
    # processor only writes the outbox and the worker sends; when disabled, the
    # compatible synchronous path claims and sends inline using the same
    # lease-guarded primitives. The two modes never run at once.
    reply_worker_enabled: bool = True
    reply_worker_poll_interval_seconds: float = Field(default=1.0, gt=0, le=3600)
    reply_worker_batch_size: int = Field(default=10, ge=1, le=100)
    reply_max_attempts: int = Field(default=3, ge=1, le=100)
    reply_lease_seconds: float = Field(default=300.0, gt=0, le=86400)
    reply_retry_base_seconds: float = Field(default=2.0, gt=0, le=86400)
    reply_retry_max_seconds: float = Field(default=3600.0, gt=0, le=86400)

    # Terminal delivery retention (P06d). Cleanup is deliberately explicit:
    # disabling uses the boolean switch, while every enabled retention window
    # is at least one day so a zero value can never erase all terminal data.
    cleanup_enabled: bool = True
    cleanup_interval_seconds: float = Field(default=3600.0, ge=60, le=604800)
    cleanup_batch_size: int = Field(default=500, ge=1, le=10000)
    event_succeeded_retention_days: int = Field(default=30, ge=1, le=3650)
    event_dead_retention_days: int = Field(default=90, ge=1, le=3650)
    outbox_sent_retention_days: int = Field(default=30, ge=1, le=3650)
    outbox_dead_retention_days: int = Field(default=90, ge=1, le=3650)

    # High-risk command confirmation (P07). risky_only policy: simple single
    # text entries write through; image / voice / batch / likely-duplicate
    # writes first create a pending_commands row and wait for the user's
    # 确认 #C-XXXXX. pending_enabled=false restores direct-write behavior as an
    # escape hatch. Expiry and retention feed the P06d Cleanup Worker.
    pending_enabled: bool = True
    pending_expires_seconds: int = Field(default=86400, ge=60, le=604800)
    pending_retention_days: int = Field(default=7, ge=1, le=365)
    pending_duplicate_window_minutes: int = Field(default=60, ge=1, le=1440)
    pending_max_list: int = Field(default=10, ge=1, le=50)

    # Recurring rules (P29): a background worker turns due active rules into
    # deterministic confirmation pendings + Feishu reminders. Rules never write
    # ledger transactions directly; only a confirmed pending becomes an entry.
    recurring_enabled: bool = True
    recurring_poll_interval_seconds: float = Field(default=300.0, ge=5, le=86400)
    recurring_batch_size: int = Field(default=10, ge=1, le=100)

    # Optional Web Dashboard. Authentication state remains in PostgreSQL; the
    # browser only receives opaque, short-lived cookies. Disabled deployments
    # retain the bot-only application surface.
    dashboard_enabled: bool = False
    dashboard_base_url: str = ""
    dashboard_session_secret: str = ""
    dashboard_admin_open_ids: str = ""
    dashboard_session_ttl_seconds: int = Field(default=28800, ge=300, le=604800)
    dashboard_oauth_state_ttl_seconds: int = Field(default=600, ge=60, le=1800)
    dashboard_cookie_secure: bool = True
    dashboard_oauth_authorize_url: str = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"

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

    @model_validator(mode="after")
    def valid_dashboard_security(self) -> "Settings":
        if not self.dashboard_enabled:
            return self
        session_secret = self.dashboard_session_secret.strip()
        if len(session_secret) < 32 or len(set(session_secret)) < 8:
            raise ValueError(
                "dashboard requires LARK_LEDGER_DASHBOARD_SESSION_SECRET "
                "with at least 32 non-trivial characters"
            )
        if not self.lark_app_id.strip() or not self.lark_app_secret.strip():
            raise ValueError("dashboard Feishu OAuth requires Lark app credentials")
        parsed = urlsplit(self.dashboard_base_url)
        if (
            not parsed.scheme
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("dashboard_base_url must be an absolute origin URL")
        if self.dashboard_cookie_secure and parsed.scheme != "https":
            raise ValueError("secure dashboard cookies require an https dashboard_base_url")
        if parsed.path not in {"", "/"}:
            raise ValueError("dashboard_base_url must not contain a path")
        return self

    @property
    def dashboard_admin_ids(self) -> frozenset[str]:
        return frozenset(
            item.strip() for item in self.dashboard_admin_open_ids.split(",") if item.strip()
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
