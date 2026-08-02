import pytest
from pydantic import ValidationError

from lark_ledger.config import EventMode, Settings


def test_default_locale_settings() -> None:
    settings = Settings(_env_file=None)
    assert settings.timezone == "Asia/Shanghai"
    assert settings.currency == "CNY"
    assert settings.event_mode is EventMode.WEBHOOK


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
