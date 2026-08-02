import pytest
from pydantic import ValidationError

from lark_ledger.config import Settings


def test_default_locale_settings() -> None:
    settings = Settings()
    assert settings.timezone == "Asia/Shanghai"
    assert settings.currency == "CNY"


def test_invalid_currency_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(currency="yuan")
