"""P07: confirmation ID generation, normalization, and display format."""

import re

import pytest

from lark_ledger.confirmation_id import (
    CONFIRMATION_PREFIX,
    ConfirmationCodeError,
    format_confirmation_ref,
    generate_confirmation_code,
    is_valid_confirmation_code,
    normalize_confirmation_code,
)
from lark_ledger.short_id import CROCKFORD_ALPHABET


def test_generate_code_has_c_prefix_and_crockford_suffix() -> None:
    for _ in range(200):
        code = generate_confirmation_code()
        assert len(code) == 6
        assert code[0] == CONFIRMATION_PREFIX
        assert set(code[1:]).issubset(set(CROCKFORD_ALPHABET))


def test_normalize_accepts_all_display_variants() -> None:
    assert normalize_confirmation_code("#C-A83F2") == "CA83F2"
    assert normalize_confirmation_code("C-A83F2") == "CA83F2"
    assert normalize_confirmation_code("#CA83F2") == "CA83F2"
    assert normalize_confirmation_code("CA83F2") == "CA83F2"
    assert normalize_confirmation_code("#c-a83f2") == "CA83F2"
    assert normalize_confirmation_code("  #C-A83F2  ") == "CA83F2"


def test_normalize_rejects_ledger_short_id_without_c_prefix() -> None:
    # A ledger short ID must never be read as a confirmation code.
    for bad in ("#A83F2", "A83F2", "#Z99K1"):
        with pytest.raises(ConfirmationCodeError):
            normalize_confirmation_code(bad)


def test_normalize_rejects_ambiguous_and_bad_characters() -> None:
    for bad in ("#C-I83F2", "#C-L83F2", "#C-O83F2", "#C-U83F2", "#C-A8F2", "#C-A83F2X", ""):
        with pytest.raises(ConfirmationCodeError):
            normalize_confirmation_code(bad)


def test_normalize_rejects_non_string() -> None:
    with pytest.raises(ConfirmationCodeError):
        normalize_confirmation_code(12345)  # type: ignore[arg-type]


def test_format_confirmation_ref() -> None:
    assert format_confirmation_ref("CA83F2") == "#C-A83F2"
    assert format_confirmation_ref("#c-a83f2") == "#C-A83F2"
    assert re.fullmatch(r"#C-[0-9A-HJKMNP-TV-Z]{5}", format_confirmation_ref("CA83F2"))


def test_is_valid_confirmation_code() -> None:
    assert is_valid_confirmation_code("CA83F2") is True
    assert is_valid_confirmation_code("#C-A83F2") is True
    assert is_valid_confirmation_code("#A83F2") is False
    assert is_valid_confirmation_code("") is False
