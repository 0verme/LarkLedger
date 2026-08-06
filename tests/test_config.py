import pytest
from pydantic import ValidationError

from lark_ledger.config import EventMode, Settings


def test_default_locale_settings() -> None:
    settings = Settings(_env_file=None)
    assert settings.timezone == "Asia/Shanghai"
    assert settings.currency == "CNY"
    assert settings.event_mode is EventMode.WEBHOOK
    assert settings.exchange_rate_api_url == "https://api.frankfurter.dev"
    assert settings.exchange_rate_cache_ttl_seconds == 3600
    assert settings.vision_model == "qwen3.7-plus"
    assert settings.transcription_model == "qwen3-asr-flash"
    assert settings.transcription_language == "zh"
    assert settings.transcription_enable_itn is True


def test_media_api_settings_are_independent() -> None:
    settings = Settings(
        _env_file=None,
        ai_api_key="text-key",
        vision_api_key="vision-key",
        vision_base_url="https://vision.example/v1",
        vision_model="vision-model",
        transcription_api_key="asr-key",
        transcription_base_url="https://asr.example/v1",
        transcription_model="asr-model",
        transcription_language="yue",
        transcription_enable_itn=False,
    )

    assert settings.ai_api_key == "text-key"
    assert settings.vision_api_key == "vision-key"
    assert settings.vision_base_url == "https://vision.example/v1"
    assert settings.vision_model == "vision-model"
    assert settings.transcription_api_key == "asr-key"
    assert settings.transcription_base_url == "https://asr.example/v1"
    assert settings.transcription_model == "asr-model"
    assert settings.transcription_language == "yue"
    assert settings.transcription_enable_itn is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [("webhook", EventMode.WEBHOOK), (" WEBSOCKET ", EventMode.WEBSOCKET)],
)
def test_event_mode_parsing(value: str, expected: EventMode) -> None:
    assert Settings(_env_file=None, event_mode=value).event_mode is expected


def test_invalid_event_mode_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, event_mode="both")


def test_invalid_currency_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, currency="yuan")


@pytest.mark.parametrize("ttl", [59, 86401])
def test_invalid_exchange_rate_cache_ttl_is_rejected(ttl: int) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, exchange_rate_cache_ttl_seconds=ttl)


def test_reply_worker_config_defaults() -> None:
    settings = Settings(_env_file=None)
    assert settings.reply_worker_enabled is True
    assert settings.reply_worker_poll_interval_seconds == 1.0
    assert settings.reply_worker_batch_size == 10
    assert settings.reply_max_attempts == 3
    assert settings.reply_lease_seconds == 300.0
    assert settings.reply_retry_base_seconds == 2.0
    assert settings.reply_retry_max_seconds == 3600.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"reply_worker_poll_interval_seconds": 0},
        {"reply_worker_batch_size": 0},
        {"reply_max_attempts": 0},
        {"reply_lease_seconds": 0},
        {"reply_retry_base_seconds": 0},
    ],
)
def test_reply_worker_config_rejects_invalid_values(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **kwargs)
