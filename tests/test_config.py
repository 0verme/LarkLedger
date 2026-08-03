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
